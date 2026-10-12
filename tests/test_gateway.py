import io
import json
import os
import subprocess
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import gateway


class RecordingRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.results.pop(0)


class FakeUnixConnection:
    def __init__(self, response):
        self.response = response
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def settimeout(self, _timeout):
        pass

    def connect(self, _path):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, _size):
        response, self.response = self.response, b""
        return response


def completed(data, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=json.dumps(data), stderr=stderr
    )


def completed_raw(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def request(operation, args=None):
    return {"version": 1, "op": operation, "args": args or {}}


def message_page(messages, page=1, page_size=10, total_pages=None, total=None):
    if total is None:
        total = len(messages)
    if total_pages is None:
        total_pages = 0 if total == 0 else 1
    return {
        "messages": messages,
        "total": total,
        "unread": len(messages),
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


def dispatch(payload, runner=None, allow_to=("studio_dot",)):
    return gateway.dispatch(
        payload,
        identity="limi_cue",
        allow_to=allow_to,
        binary="/srv/agent-chat/thrum",
        hub="/srv/agent-chat/hub",
        runner=runner or RecordingRunner([]),
    )


class RequestValidationTests(unittest.TestCase):
    def test_request_has_no_client_selected_identity_or_extra_fields(self):
        for extra in ({"agent": "limi_cue"}, {"binary": "/tmp/evil"}):
            with self.subTest(extra=extra):
                runner = RecordingRunner([])
                payload = request("ping")
                payload.update(extra)

                response = dispatch(payload, runner)

                self.assertEqual(response["error"], "invalid request")
                self.assertEqual(runner.calls, [])

    def test_rejects_absent_operations_and_unknown_operations(self):
        for operation in ("register", "status", "delete", "thread_reply"):
            with self.subTest(operation=operation):
                response = dispatch(request(operation))
                self.assertEqual(response["error"], "invalid request")

    def test_rejects_wrong_protocol_and_argument_types(self):
        cases = [
            {"version": True, "op": "ping", "args": {}},
            {"version": 2, "op": "ping", "args": {}},
            {"version": 1, "op": [], "args": {}},
            {"version": 1, "op": "ping", "args": []},
            request("inbox", {"unread": 1}),
            request("wait", {"timeout": True}),
            request("wait", {"timeout": 0}),
            request("wait", {"timeout": 51}),
            request("get", {"message_id": "--all"}),
            request("send", {"to": "bad;name", "body": "hello"}),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(dispatch(payload)["error"], "invalid request")

    def test_rejects_operation_specific_extra_keys(self):
        response = dispatch(request("inbox", {"unread": True, "limit": 9999}))
        self.assertEqual(response["error"], "invalid request")

    def test_body_limit_counts_utf8_bytes(self):
        accepted = "я" * (gateway.MAX_BODY_BYTES // 2)
        runner = RecordingRunner([completed({"message_id": "msg_OK"})])

        response = dispatch(
            request("send", {"to": "studio_dot", "body": accepted}), runner
        )
        rejected = dispatch(
            request("send", {"to": "studio_dot", "body": accepted + "я"})
        )

        self.assertTrue(response["ok"])
        self.assertEqual(rejected["error"], "invalid request")

    def test_rejects_body_that_is_not_valid_utf8_text(self):
        response = dispatch(
            request("send", {"to": "studio_dot", "body": "\ud800"})
        )

        self.assertEqual(response["error"], "invalid request")


class ProtocolBoundaryTests(unittest.TestCase):
    def test_rpc_rejects_wrong_id_error_and_oversized_frame(self):
        responses = [
            b'{"jsonrpc":"2.0","id":2,"result":{}}\n',
            b'{"jsonrpc":"2.0","id":1,"error":{"code":-1}}\n',
            b"{" + b" " * gateway.MAX_RPC_RESPONSE_BYTES + b"}\n",
        ]
        with tempfile.TemporaryDirectory() as hub:
            socket_dir = Path(hub) / ".thrum" / "var"
            socket_dir.mkdir(parents=True)
            socket_path = socket_dir / "thrum.sock"
            marker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            marker.bind(str(socket_path))
            marker.close()
            for response in responses:
                with self.subTest(size=len(response)), mock.patch(
                    "gateway.socket.socket", return_value=FakeUnixConnection(response)
                ):
                    with self.assertRaises(gateway.ServiceError):
                        gateway._rpc_call(hub, "message.list", {})

    def test_identity_file_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as hub, tempfile.TemporaryDirectory() as outside:
            identities = Path(hub) / ".thrum" / "identities"
            identities.mkdir(parents=True)
            target = Path(outside) / "identity.json"
            target.write_text(
                json.dumps({"agent": {"Name": "limi_cue", "Role": "agent_chat_limi"}}),
                encoding="utf-8",
            )
            (identities / "limi_cue.json").symlink_to(target)

            with self.assertRaises(gateway.ServiceError):
                gateway._identity_role("limi_cue", hub)

    def test_socket_parent_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as hub, tempfile.TemporaryDirectory() as outside:
            thrum = Path(hub) / ".thrum"
            thrum.mkdir()
            outside_var = Path(outside) / "var"
            outside_var.mkdir()
            (thrum / "var").symlink_to(outside_var, target_is_directory=True)
            marker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            marker.bind(str(outside_var / "thrum.sock"))
            marker.close()

            with self.assertRaises(gateway.ServiceError):
                gateway._rpc_call(hub, "message.list", {})

    def test_original_command_must_match_exactly(self):
        raw = json.dumps(request("ping")).encode("utf-8")
        for command in (None, "", "id", "agent-chat-v1 ", " agent-chat-v1"):
            with self.subTest(command=command):
                response = gateway.handle_request_bytes(
                    raw,
                    original_command=command,
                    identity="limi_cue",
                    allow_to=(),
                    binary="/srv/agent-chat/thrum",
                    hub="/srv/agent-chat/hub",
                    runner=RecordingRunner([]),
                )
                self.assertEqual(response["error"], "command rejected")

    def test_rejects_malformed_trailing_and_non_utf8_json(self):
        cases = [b"{", b"{}{}", b"\xff"]
        for raw in cases:
            with self.subTest(raw=raw):
                response = gateway.handle_request_bytes(
                    raw,
                    original_command="agent-chat-v1",
                    identity="limi_cue",
                    allow_to=(),
                    binary="/srv/agent-chat/thrum",
                    hub="/srv/agent-chat/hub",
                    runner=RecordingRunner([]),
                )
                self.assertEqual(response["error"], "invalid request")

    def test_rejects_duplicate_json_keys(self):
        raw = (
            b'{"version":2,"version":1,"op":"ping","args":{}}'
        )

        response = gateway.handle_request_bytes(
            raw,
            original_command="agent-chat-v1",
            identity="limi_cue",
            allow_to=(),
            binary="/srv/agent-chat/thrum",
            hub="/srv/agent-chat/hub",
            runner=RecordingRunner([]),
        )

        self.assertEqual(response["error"], "invalid request")

    def test_rejects_non_standard_json_constants(self):
        raw = b'{"version":1,"op":"ping","args":{"value":NaN}}'

        response = gateway.handle_request_bytes(
            raw,
            original_command="agent-chat-v1",
            identity="limi_cue",
            allow_to=(),
            binary="/srv/agent-chat/thrum",
            hub="/srv/agent-chat/hub",
            runner=RecordingRunner([]),
        )

        self.assertEqual(response["error"], "invalid request")

    def test_rejects_request_larger_than_64_kib(self):
        response = gateway.handle_request_bytes(
            b" " * (gateway.MAX_REQUEST_BYTES + 1),
            original_command="agent-chat-v1",
            identity="limi_cue",
            allow_to=(),
            binary="/srv/agent-chat/thrum",
            hub="/srv/agent-chat/hub",
            runner=RecordingRunner([]),
        )
        self.assertEqual(response["error"], "request too large")

    def test_deep_json_returns_stable_invalid_request(self):
        raw = ("[" * 1500 + "0" + "]" * 1500).encode("ascii")

        response = gateway.handle_request_bytes(
            raw,
            original_command="agent-chat-v1",
            identity="limi_cue",
            allow_to=(),
            binary="/srv/agent-chat/thrum",
            hub="/srv/agent-chat/hub",
            runner=RecordingRunner([]),
        )

        self.assertEqual(
            response,
            {"ok": False, "data": None, "error": "invalid request"},
        )

    def test_pty_environment_is_rejected(self):
        output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"SSH_ORIGINAL_COMMAND": "agent-chat-v1", "SSH_TTY": "/dev/ttys001"},
            clear=True,
        ), mock.patch.object(gateway.sys, "stdin", io.StringIO("{}")), mock.patch.object(
            gateway.sys, "stdout", output
        ):
            exit_code = gateway.main(
                [
                    "--identity",
                    "limi_cue",
                    "--binary",
                    "/srv/agent-chat/thrum",
                    "--hub",
                    "/srv/agent-chat/hub",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["error"], "command rejected")

    def test_valid_forced_command_returns_protocol_errors_on_stdout(self):
        output = io.StringIO()
        payload = json.dumps(request("status"))
        with mock.patch.dict(
            os.environ,
            {"SSH_ORIGINAL_COMMAND": "agent-chat-v1"},
            clear=True,
        ), mock.patch.object(
            gateway.sys, "stdin", io.StringIO(payload)
        ), mock.patch.object(gateway.sys, "stdout", output):
            exit_code = gateway.main(
                [
                    "--identity",
                    "limi_cue",
                    "--binary",
                    "/srv/agent-chat/thrum",
                    "--hub",
                    "/srv/agent-chat/hub",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["error"], "invalid request")

    def test_stdin_without_eof_hits_deadline_and_returns_json(self):
        script = (
            "import gateway; "
            "gateway.INPUT_TIMEOUT_SECONDS = 0.2; "
            "raise SystemExit(gateway.main(["
            "'--identity','limi_cue',"
            "'--binary','/srv/agent-chat/thrum',"
            "'--hub','/srv/agent-chat/hub']))"
        )
        environment = dict(os.environ)
        environment["SSH_ORIGINAL_COMMAND"] = "agent-chat-v1"
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=str(Path(gateway.__file__).parent),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            returncode = process.wait(timeout=2)
            stdout = process.stdout.read()
            stderr = process.stderr.read()
        finally:
            if process.stdin:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

        self.assertEqual(returncode, 0)
        self.assertEqual(json.loads(stdout)["error"], "invalid request")
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn(str(Path(gateway.__file__).parent), stderr)


class CommandDispatchTests(unittest.TestCase):
    def test_membership_scans_all_read_only_rpc_pages(self):
        pages = [
            {
                "messages": [{"message_id": "msg_FIRST"}],
                "total": 101,
                "unread": 0,
                "page": 1,
                "page_size": 100,
                "total_pages": 2,
            },
            {
                "messages": [{"message_id": "msg_OLD"}],
                "total": 101,
                "unread": 0,
                "page": 2,
                "page_size": 100,
                "total_pages": 2,
            },
        ]
        with mock.patch("gateway._identity_role", return_value="agent_chat_limi"), mock.patch(
            "gateway._rpc_call", side_effect=pages
        ) as rpc:
            gateway._require_inbox_membership(
                "msg_OLD", "limi_cue", "/srv/thrum", "/srv/hub", RecordingRunner([])
            )

        self.assertEqual(rpc.call_count, 2)
        for index, call in enumerate(rpc.call_args_list, 1):
            method, params = call.args[1:]
            self.assertEqual(method, "message.list")
            self.assertEqual(params["caller_agent_id"], "limi_cue")
            self.assertEqual(params["caller_mention_role"], "agent_chat_limi")
            self.assertEqual(params["for_agent"], "limi_cue")
            self.assertEqual(params["for_agent_role"], "agent_chat_limi")
            self.assertEqual(params["page_size"], 100)
            self.assertEqual(params["page"], index)
            self.assertEqual(params["sort_by"], "created_at")
            self.assertEqual(params["sort_order"], "asc")

    def test_inbox_and_sent_listing_use_read_only_rpc(self):
        empty = {
            "messages": [],
            "total": 0,
            "unread": 0,
            "page": 1,
            "page_size": 10,
            "total_pages": 0,
        }
        runner = RecordingRunner([])
        with mock.patch("gateway._identity_role", return_value="agent_chat_limi"), mock.patch(
            "gateway._rpc_call", return_value=empty
        ) as rpc:
            inbox = dispatch(request("inbox"), runner)
            sent = dispatch(request("sent"), runner)

        self.assertTrue(inbox["ok"])
        self.assertTrue(sent["ok"])
        self.assertEqual([call.args[1] for call in rpc.call_args_list], [
            "message.list", "message.outbox"
        ])
        self.assertEqual(runner.calls, [])

    def test_ping_does_not_invoke_thrum(self):
        runner = RecordingRunner([])
        response = dispatch(request("ping"), runner)
        self.assertEqual(
            response,
            {
                "ok": True,
                "data": {"protocol": 1, "identity": "limi_cue"},
                "error": None,
            },
        )
        self.assertEqual(runner.calls, [])

    def test_send_requires_server_allowlist_and_uses_stdin(self):
        body = "text\n'\" $(touch /tmp/nope) `id`"
        runner = RecordingRunner([completed({"message_id": "msg_SENT"})])

        denied = dispatch(
            request("send", {"to": "test_other", "body": body}), runner
        )
        allowed = dispatch(
            request("send", {"to": "studio_dot", "body": body}), runner
        )

        self.assertEqual(denied["error"], "access denied")
        self.assertTrue(allowed["ok"])
        self.assertEqual(len(runner.calls), 1)
        argv, kwargs = runner.calls[0]
        self.assertEqual(
            argv,
            [
                "/srv/agent-chat/thrum",
                "send",
                "--to",
                "@studio_dot",
                "--format",
                "plain",
                "--stdin",
                "--json",
            ],
        )
        self.assertEqual(kwargs["input"], body)
        self.assertNotIn(body, argv)
        self.assertFalse(kwargs.get("shell", False))

    def test_rpc_identity_and_role_are_server_fixed(self):
        runner = RecordingRunner([])
        with mock.patch.dict(
            os.environ,
            {"THRUM_NAME": "attacker", "AGENT_CHAT_THRUM_BINARY": "/tmp/evil"},
            clear=False,
        ), mock.patch("gateway._identity_role", return_value="agent_chat_limi"), mock.patch(
            "gateway._rpc_call", return_value=message_page([])
        ) as rpc:
            response = dispatch(request("inbox"), runner)

        self.assertTrue(response["ok"])
        self.assertEqual(rpc.call_args.args[0], "/srv/agent-chat/hub")
        self.assertEqual(rpc.call_args.args[2]["caller_agent_id"], "limi_cue")
        self.assertEqual(rpc.call_args.args[2]["caller_mention_role"], "agent_chat_limi")
        self.assertEqual(runner.calls, [])

    def test_get_checks_inbox_membership_before_fetching(self):
        runner = RecordingRunner([])
        with mock.patch("gateway._identity_role", return_value="agent_chat_limi"), mock.patch(
            "gateway._rpc_call",
            side_effect=[
                message_page([{"message_id": "msg_OWN"}], page_size=100),
                {"message": {"message_id": "msg_OWN", "body": {"content": "hello"}}},
            ],
        ) as rpc:
            response = dispatch(request("get", {"message_id": "msg_OWN"}), runner)

        self.assertTrue(response["ok"])
        self.assertEqual(rpc.call_args_list[1].args[1], "message.get")
        self.assertEqual(rpc.call_args_list[1].args[2], {"message_id": "msg_OWN"})
        self.assertEqual(runner.calls, [])

    def test_get_allows_sent_membership_after_inbox_miss(self):
        runner = RecordingRunner([])
        with mock.patch("gateway._identity_role", return_value="agent_chat_limi"), mock.patch(
            "gateway._rpc_call",
            side_effect=[
                message_page([], page_size=100),
                message_page([{"message_id": "msg_SENT"}], page_size=100),
                {"message": {"message_id": "msg_SENT", "body": {"content": "outbound"}}},
            ],
        ) as rpc:
            response = dispatch(request("get", {"message_id": "msg_SENT"}), runner)

        self.assertTrue(response["ok"])
        self.assertEqual(rpc.call_args_list[1].args[1], "message.outbox")
        self.assertEqual(rpc.call_args_list[2].args[1], "message.get")
        self.assertEqual(runner.calls, [])

    def test_get_foreign_id_never_invokes_arbitrary_get(self):
        runner = RecordingRunner([])
        with mock.patch("gateway._identity_role", return_value="agent_chat_limi"), mock.patch(
            "gateway._rpc_call",
            side_effect=[message_page([], page_size=100), message_page([], page_size=100)],
        ):
            response = dispatch(request("get", {"message_id": "msg_FOREIGN"}), runner)

        self.assertEqual(response["error"], "access denied")
        self.assertEqual(runner.calls, [])

    def test_read_and_reply_require_inbox_membership(self):
        for operation, args in (
            ("read", {"message_id": "msg_SENT"}),
            ("reply", {"message_id": "msg_SENT", "body": "no"}),
        ):
            with self.subTest(operation=operation):
                runner = RecordingRunner([])
                with mock.patch("gateway._identity_role", return_value="agent_chat_limi"), mock.patch(
                    "gateway._rpc_call", return_value=message_page([], page_size=100)
                ):
                    response = dispatch(request(operation, args), runner)
                self.assertEqual(response["error"], "access denied")
                self.assertEqual(runner.calls, [])

    def test_reply_to_inbox_message_uses_native_reply_and_literal_stdin(self):
        body = "answer\n$(id) `touch /tmp/nope`"
        runner = RecordingRunner([completed({
            "message_id": "msg_REPLY", "reply_to": "msg_IN", "thread_id": "thr_THREAD"
        })])
        with mock.patch("gateway._identity_role", return_value="agent_chat_limi"), mock.patch(
            "gateway._rpc_call",
            return_value=message_page([{"message_id": "msg_IN"}], page_size=100),
        ):
            response = dispatch(
                request("reply", {"message_id": "msg_IN", "body": body}), runner
            )

        self.assertTrue(response["ok"])
        argv, kwargs = runner.calls[0]
        self.assertEqual(
            argv[1:],
            ["reply", "msg_IN", "--format", "plain", "--stdin", "--json"],
        )
        self.assertEqual(kwargs["input"], body)
        self.assertNotIn(body, argv)

    def test_unknown_membership_schema_fails_closed(self):
        for data in (
            {"unexpected": []},
            {"messages": "not-a-list"},
            {"messages": [{"message_id": "bad"}]},
        ):
            with self.subTest(data=data):
                runner = RecordingRunner([])
                with mock.patch("gateway._identity_role", return_value="agent_chat_limi"), mock.patch(
                    "gateway._rpc_call", return_value=data
                ):
                    response = dispatch(
                        request("read", {"message_id": "msg_OWN"}), runner
                    )
                self.assertEqual(response["error"], "service unavailable")
                self.assertEqual(runner.calls, [])

    def test_thrum_failure_is_sanitized(self):
        runner = RecordingRunner([])
        with mock.patch("gateway._identity_role", return_value="agent_chat_limi"), mock.patch(
            "gateway._rpc_call", side_effect=gateway.ServiceError()
        ):
            response = dispatch(request("inbox"), runner)

        self.assertEqual(
            response,
            {"ok": False, "data": None, "error": "service unavailable"},
        )


class WaitTests(unittest.TestCase):
    def test_wait_returns_existing_unread_without_native_wait(self):
        message = {"message_id": "msg_READY", "body": "ready"}
        runner = RecordingRunner([])

        with mock.patch("gateway._identity_role", return_value="agent_chat_limi"), mock.patch(
            "gateway._rpc_call", return_value=message_page([message])
        ):
            response = dispatch(request("wait", {"timeout": 5}), runner)

        self.assertEqual(
            response["data"], {"status": "received", "messages": [message]}
        )
        self.assertEqual(runner.calls, [])

    def test_wait_uses_native_moving_window_and_rechecks_unread(self):
        message = {"message_id": "msg_NEW", "body": "new"}
        runner = RecordingRunner([completed({"status": "received"})])

        with mock.patch("gateway.time.monotonic", side_effect=[100.0, 100.0]), mock.patch(
            "gateway._identity_role", return_value="agent_chat_limi"
        ), mock.patch(
            "gateway._rpc_call", side_effect=[message_page([]), message_page([message])]
        ):
            response = dispatch(request("wait", {"timeout": 7}), runner)

        self.assertEqual(
            runner.calls[0][0][1:],
            [
                "wait",
                "--timeout",
                "7000ms",
                "--json",
            ],
        )
        self.assertEqual(runner.calls[0][1]["timeout"], 8.0)
        self.assertEqual(response["data"]["status"], "received")

    def test_wait_accepts_native_rc1_empty_timeout_then_rechecks(self):
        timeout_notice = (
            "NO_MESSAGES_TIMEOUT "
            + chr(0x2014)
            + " re-run thrum wait to continue listening"
        )
        for stderr in ("", timeout_notice):
            with self.subTest(stderr=bool(stderr)):
                runner = RecordingRunner([completed_raw(returncode=1, stderr=stderr)])
                with mock.patch(
                    "gateway.time.monotonic", side_effect=[100.0, 100.0, 101.0]
                ), mock.patch(
                    "gateway._identity_role", return_value="agent_chat_limi"
                ), mock.patch(
                    "gateway._rpc_call", side_effect=[message_page([]), message_page([])]
                ):
                    response = dispatch(request("wait", {"timeout": 1}), runner)

                self.assertEqual(
                    response["data"], {"status": "timeout", "messages": []}
                )
                self.assertEqual(len(runner.calls), 1)

    def test_wait_continues_after_non_unread_event_until_deadline(self):
        runner = RecordingRunner([completed({"status": "received"}), completed_raw(returncode=1)])
        with mock.patch(
            "gateway.time.monotonic",
            side_effect=[100.0, 100.0, 100.2, 100.2, 107.0],
        ), mock.patch(
            "gateway._identity_role", return_value="agent_chat_limi"
        ), mock.patch(
            "gateway._rpc_call",
            side_effect=[message_page([]), message_page([]), message_page([])],
        ):
            response = dispatch(request("wait", {"timeout": 7}), runner)

        wait_calls = [call for call in runner.calls if call[0][1] == "wait"]
        self.assertEqual(len(wait_calls), 2)
        self.assertNotIn("--after", wait_calls[0][0])
        self.assertNotIn("--after", wait_calls[1][0])
        self.assertEqual(response["data"], {"status": "timeout", "messages": []})

    def test_wait_does_not_accept_other_failures_or_rc0_empty(self):
        for native in (
            completed_raw(returncode=2),
            completed_raw(returncode=0),
            completed_raw(returncode=1, stderr="invalid wait arguments"),
            completed_raw(
                returncode=1,
                stderr=(
                    "NO_MESSAGES_TIMEOUT X re-run thrum wait to continue listening"
                ),
            ),
        ):
            with self.subTest(returncode=native.returncode):
                runner = RecordingRunner([native])
                with mock.patch(
                    "gateway.time.monotonic", side_effect=[100.0, 100.0]
                ), mock.patch(
                    "gateway._identity_role", return_value="agent_chat_limi"
                ), mock.patch(
                    "gateway._rpc_call", return_value=message_page([])
                ):
                    response = dispatch(request("wait", {"timeout": 1}), runner)
                self.assertEqual(response["error"], "service unavailable")
                self.assertEqual(len(runner.calls), 1)


@unittest.skipUnless(
    os.environ.get("AGENT_CHAT_RESTRICTED_THRUM_BINARY"),
    "AGENT_CHAT_RESTRICTED_THRUM_BINARY is required",
)
class RestrictedThrumIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = str(
            Path(os.environ["AGENT_CHAT_RESTRICTED_THRUM_BINARY"]).resolve()
        )
        cls.temp = tempfile.TemporaryDirectory(prefix="acr-", dir="/tmp")
        cls.hub = cls.temp.name
        subprocess.run(
            ["git", "init", "--quiet", cls.hub],
            check=True,
            text=True,
            capture_output=True,
        )
        initialized = subprocess.run(
            [
                cls.binary,
                "init",
                "--non-interactive",
                "--name",
                "test_restricted_bootstrap",
                "--role",
                "test_restricted_bootstrap",
                "--module",
                "agent-chat-restricted-tests",
                "--roles=skip",
                "--runtime",
                "cli-only",
                "--no-daemon",
                "--stealth",
            ],
            cwd=cls.hub,
            check=False,
            text=True,
            capture_output=True,
            env={**os.environ, "THRUM_NO_HINTS": "1"},
        )
        if initialized.returncode != 0:
            cls.temp.cleanup()
            raise RuntimeError("isolated Thrum init failed")
        for identity in (
            "test_restricted_limi",
            "test_restricted_sender",
            "test_restricted_other",
        ):
            registered = cls.run_thrum(
                identity,
                [
                    "quickstart",
                    "--name",
                    identity,
                    "--role",
                    "agent_chat_" + identity,
                    "--module",
                    "agent-chat",
                    "--runtime",
                    "cli-only",
                    "--no-init",
                    "--no-agent-pid",
                    "--intent",
                    "restricted integration test",
                    "--json",
                ],
            )
            if registered.returncode != 0:
                cls.stop_daemon()
                cls.temp.cleanup()
                raise RuntimeError("isolated Thrum registration failed")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "hub"):
            cls.stop_daemon()
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    @classmethod
    def stop_daemon(cls):
        subprocess.run(
            [cls.binary, "daemon", "stop"],
            cwd=cls.hub,
            check=False,
            text=True,
            capture_output=True,
            env={**os.environ, "THRUM_NO_HINTS": "1"},
        )

    @classmethod
    def run_thrum(cls, identity, arguments, body=None):
        return subprocess.run(
            [cls.binary] + arguments,
            cwd=cls.hub,
            env={
                **os.environ,
                "THRUM_NAME": identity,
                "THRUM_NO_HINTS": "1",
            },
            input=body,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )

    @classmethod
    def direct_send(cls, sender, recipient, body):
        result = cls.run_thrum(
            sender,
            [
                "send",
                "--to",
                "@" + recipient,
                "--format",
                "plain",
                "--stdin",
                "--json",
            ],
            body=body,
        )
        if result.returncode != 0:
            raise RuntimeError("isolated Thrum send failed")
        return json.loads(result.stdout)["message_id"]

    @classmethod
    def restricted_call(cls, operation, args=None):
        return gateway.dispatch(
            request(operation, args),
            identity="test_restricted_limi",
            allow_to=("test_restricted_sender",),
            binary=cls.binary,
            hub=cls.hub,
        )

    def setUp(self):
        for identity in (
            "test_restricted_limi",
            "test_restricted_sender",
            "test_restricted_other",
        ):
            while True:
                inbox = self.run_thrum(
                    identity,
                    ["inbox", "--unread", "--page-size", "100", "--json"],
                )
                self.assertEqual(inbox.returncode, 0, inbox.stderr)
                messages = json.loads(inbox.stdout)["messages"]
                if not messages:
                    break
                for message in messages:
                    marked = self.run_thrum(
                        identity,
                        ["message", "read", message["message_id"], "--json"],
                    )
                    self.assertEqual(marked.returncode, 0, marked.stderr)

    def unread_ids(self, identity):
        inbox = self.run_thrum(
            identity,
            ["inbox", "--unread", "--page-size", "100", "--json"],
        )
        self.assertEqual(inbox.returncode, 0, inbox.stderr)
        return {item["message_id"] for item in json.loads(inbox.stdout)["messages"]}

    def test_actual_schemas_acl_and_literal_bodies(self):
        incoming_id = self.direct_send(
            "test_restricted_sender", "test_restricted_limi", "incoming"
        )
        foreign_id = self.direct_send(
            "test_restricted_sender", "test_restricted_other", "foreign"
        )

        inbox = self.restricted_call("inbox")
        self.assertTrue(inbox["ok"], inbox)
        self.assertIn(
            incoming_id,
            {item["message_id"] for item in inbox["data"]["messages"]},
        )

        fetched = self.restricted_call("get", {"message_id": incoming_id})
        self.assertTrue(fetched["ok"], fetched)
        self.assertEqual(fetched["data"]["message"]["body"]["content"], "incoming")

        for operation, args in (
            ("get", {"message_id": foreign_id}),
            ("read", {"message_id": foreign_id}),
            ("reply", {"message_id": foreign_id, "body": "must not send"}),
        ):
            with self.subTest(operation=operation):
                denied = self.restricted_call(operation, args)
                self.assertEqual(denied["error"], "access denied")

        marker = Path(self.hub) / "shell-marker"
        literal_body = "literal $(touch %s) `id`\nUnicode: привет" % marker
        sent = self.restricted_call(
            "send", {"to": "test_restricted_sender", "body": literal_body}
        )
        self.assertTrue(sent["ok"], sent)
        self.assertFalse(marker.exists())

        sent_list = self.restricted_call("sent")
        self.assertTrue(sent_list["ok"], sent_list)
        self.assertIn(
            sent["data"]["message_id"],
            {item["message_id"] for item in sent_list["data"]["messages"]},
        )

        reply = self.restricted_call(
            "reply", {"message_id": incoming_id, "body": literal_body}
        )
        self.assertTrue(reply["ok"], reply)
        fetched_reply = self.restricted_call(
            "get", {"message_id": reply["data"]["message_id"]}
        )
        self.assertTrue(fetched_reply["ok"], fetched_reply)
        reply_message = fetched_reply["data"]["message"]
        self.assertEqual(
            [
                ref["value"]
                for ref in reply_message["refs"]
                if ref["type"] == "reply_to"
            ],
            [incoming_id],
        )
        self.assertRegex(reply_message["thread_id"], r"^thr_[0-9A-Z]+$")
        self.assertFalse(marker.exists())

        marked = self.restricted_call("read", {"message_id": incoming_id})
        self.assertTrue(marked["ok"], marked)

        other_unread = self.run_thrum(
            "test_restricted_other", ["inbox", "--unread", "--json"]
        )
        self.assertEqual(other_unread.returncode, 0)
        self.assertIn(
            foreign_id,
            {
                item["message_id"]
                for item in json.loads(other_unread.stdout)["messages"]
            },
        )

    def test_listing_is_read_only_and_acl_scans_beyond_first_page(self):
        message_ids = [
            self.direct_send(
                "test_restricted_sender",
                "test_restricted_limi",
                "page item %02d" % index,
            )
            for index in range(12)
        ]
        oldest_id = message_ids[0]

        listed = self.restricted_call("inbox")
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(
            self.unread_ids("test_restricted_limi"), set(message_ids)
        )

        denied = self.restricted_call("get", {"message_id": "msg_00000000000000000000000000"})
        self.assertEqual(denied["error"], "access denied")
        self.assertEqual(
            self.unread_ids("test_restricted_limi"), set(message_ids)
        )

        fetched = self.restricted_call("get", {"message_id": oldest_id})
        self.assertTrue(fetched["ok"], fetched)
        self.assertEqual(
            self.unread_ids("test_restricted_limi"), set(message_ids)
        )

        marked = self.restricted_call("read", {"message_id": oldest_id})
        self.assertTrue(marked["ok"], marked)
        self.assertNotIn(oldest_id, self.unread_ids("test_restricted_limi"))

        replied = self.restricted_call(
            "reply", {"message_id": oldest_id, "body": "reply after explicit read"}
        )
        self.assertTrue(replied["ok"], replied)

    def test_wait_ignores_old_read_event_sees_new_arrival_and_times_out(self):
        old_id = self.direct_send(
            "test_restricted_sender", "test_restricted_limi", "old and read"
        )
        marked = self.restricted_call("read", {"message_id": old_id})
        self.assertTrue(marked["ok"], marked)

        started = time.monotonic()
        old_wait = self.restricted_call("wait", {"timeout": 1})
        elapsed = time.monotonic() - started
        self.assertEqual(old_wait["data"], {"status": "timeout", "messages": []})
        self.assertGreaterEqual(elapsed, 0.7)

        result = {}

        def wait_for_new():
            result["response"] = self.restricted_call("wait", {"timeout": 4})

        thread = threading.Thread(target=wait_for_new)
        thread.start()
        time.sleep(0.4)
        new_id = self.direct_send(
            "test_restricted_sender", "test_restricted_limi", "new arrival"
        )
        thread.join(timeout=7)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["response"]["data"]["status"], "received")
        self.assertIn(
            new_id,
            {
                message["message_id"]
                for message in result["response"]["data"]["messages"]
            },
        )
        self.assertTrue(
            self.restricted_call("read", {"message_id": new_id})["ok"]
        )

        no_data = self.restricted_call("wait", {"timeout": 1})
        self.assertEqual(no_data["data"], {"status": "timeout", "messages": []})


if __name__ == "__main__":
    unittest.main()
