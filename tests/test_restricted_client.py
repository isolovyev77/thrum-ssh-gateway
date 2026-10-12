import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import restricted_client


class RecordingRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.result


def completed(response, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=json.dumps(response), stderr=stderr
    )


class RequestTests(unittest.TestCase):
    def test_build_request_has_no_agent_field(self):
        self.assertEqual(
            restricted_client.build_request("ping", {}),
            {"version": 1, "op": "ping", "args": {}},
        )

    def test_client_exposes_only_restricted_operations(self):
        for operation in ("register", "status", "delete", "thread_reply"):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "operation"):
                    restricted_client.build_request(operation, {})

    def test_client_validates_types_ids_wait_and_body_size(self):
        cases = [
            ("inbox", {"unread": 1}),
            ("get", {"message_id": "--all"}),
            ("wait", {"timeout": True}),
            ("wait", {"timeout": 0}),
            ("wait", {"timeout": 51}),
            ("send", {"to": "bad;name", "body": "hello"}),
            ("reply", {"message_id": "msg_OK", "body": "я" * 16385}),
            ("send", {"to": "studio_dot", "body": "\ud800"}),
        ]
        for operation, args in cases:
            with self.subTest(operation=operation, args=args):
                with self.assertRaises(ValueError):
                    restricted_client.build_request(operation, args)


class TransportTests(unittest.TestCase):
    def test_transport_uses_safe_ssh_flags_alias_and_fixed_remote_command(self):
        response = {"ok": True, "data": {"protocol": 1}, "error": None}
        runner = RecordingRunner(completed(response))

        actual = restricted_client.transport(
            restricted_client.build_request("ping", {}),
            ssh_alias="cue-agent-chat",
            runner=runner,
        )

        argv, kwargs = runner.calls[0]
        self.assertEqual(
            argv,
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "cue-agent-chat",
                "agent-chat-v1",
            ],
        )
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(actual, response)

    def test_transport_uses_separate_local_ssh_config_as_argv(self):
        response = {"ok": True, "data": {"protocol": 1}, "error": None}
        runner = RecordingRunner(completed(response))
        request = restricted_client.build_request("ping", {})

        restricted_client.transport(
            request,
            ssh_alias="cue-agent-chat",
            ssh_config="/tmp/agent-chat-ssh-config",
            runner=runner,
        )

        argv, kwargs = runner.calls[0]
        self.assertEqual(
            argv,
            [
                "ssh",
                "-F",
                "/tmp/agent-chat-ssh-config",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "cue-agent-chat",
                "agent-chat-v1",
            ],
        )
        self.assertNotIn("/tmp/agent-chat-ssh-config", kwargs["input"])
        self.assertFalse(kwargs.get("shell", False))

    def test_ssh_config_must_be_a_nonempty_local_path(self):
        for path in ("", "\x00bad"):
            with self.subTest(path=path):
                runner = RecordingRunner(completed({}))
                with self.assertRaisesRegex(ValueError, "SSH config"):
                    restricted_client.transport(
                        restricted_client.build_request("ping", {}),
                        ssh_config=path,
                        runner=runner,
                    )
                self.assertEqual(runner.calls, [])

    def test_alias_is_validated_before_ssh(self):
        for alias in ("-oProxyCommand=sh", "host name", "a;id", ""):
            with self.subTest(alias=alias):
                runner = RecordingRunner(completed({}))
                with self.assertRaisesRegex(ValueError, "alias"):
                    restricted_client.transport(
                        restricted_client.build_request("ping", {}),
                        ssh_alias=alias,
                        runner=runner,
                    )
                self.assertEqual(runner.calls, [])

    def test_transport_revalidates_request_before_ssh(self):
        runner = RecordingRunner(completed({}))
        request = {
            "version": 1,
            "agent": "attacker",
            "op": "ping",
            "args": {},
        }

        with self.assertRaisesRegex(ValueError, "request"):
            restricted_client.transport(request, runner=runner)

        self.assertEqual(runner.calls, [])

    def test_shell_payload_is_only_in_json_stdin(self):
        body = "line\n'\" $(touch /tmp/nope) `id`"
        response = {"ok": True, "data": {"message_id": "msg_OK"}, "error": None}
        runner = RecordingRunner(completed(response))
        payload = restricted_client.build_request(
            "send", {"to": "studio_dot", "body": body}
        )

        restricted_client.transport(payload, runner=runner)

        argv, kwargs = runner.calls[0]
        self.assertNotIn(body, argv)
        self.assertEqual(json.loads(kwargs["input"]), payload)

    def test_transport_failure_is_not_retried_or_leaked(self):
        runner = RecordingRunner(
            subprocess.CompletedProcess(
                args=[],
                returncode=255,
                stdout="",
                stderr="private host and /private/internal/key",
            )
        )

        with self.assertRaisesRegex(RuntimeError, "transport failed") as raised:
            restricted_client.transport(
                restricted_client.build_request(
                    "reply", {"message_id": "msg_OK", "body": "once"}
                ),
                runner=runner,
            )

        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("/private/internal", str(raised.exception))

    def test_rejects_malformed_or_extra_response_fields(self):
        responses = [
            "not json",
            json.dumps(
                {"ok": True, "data": {}, "error": None, "internal_path": "/secret"}
            ),
            json.dumps({"ok": "yes", "data": {}, "error": None}),
        ]
        for stdout in responses:
            with self.subTest(stdout=stdout):
                runner = RecordingRunner(
                    subprocess.CompletedProcess(
                        args=[], returncode=0, stdout=stdout, stderr=""
                    )
                )
                with self.assertRaisesRegex(RuntimeError, "invalid response"):
                    restricted_client.transport(
                        restricted_client.build_request("ping", {}), runner=runner
                    )

    def test_deep_response_is_rejected_without_recursion_error(self):
        deep_json = "[" * 1500 + "0" + "]" * 1500
        runner = RecordingRunner(
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout=deep_json, stderr=""
            )
        )

        with self.assertRaisesRegex(RuntimeError, "invalid response"):
            restricted_client.transport(
                restricted_client.build_request("ping", {}), runner=runner
            )


class CommandLineTests(unittest.TestCase):
    def test_send_reads_local_body_file_without_sending_path(self):
        body = "literal\n$(id) `touch /tmp/nope`\n"
        response = {"ok": True, "data": {"message_id": "msg_OK"}, "error": None}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.txt"
            path.write_text(body, encoding="utf-8")
            output = io.StringIO()
            with mock.patch(
                "restricted_client.transport", return_value=response
            ) as transport, redirect_stdout(output):
                exit_code = restricted_client.main(
                    [
                        "--ssh-alias",
                        "cue-agent-chat",
                        "--ssh-config",
                        "/tmp/agent-chat-ssh-config",
                        "send",
                        "--to",
                        "studio_dot",
                        "--body-file",
                        str(path),
                    ]
                )

        sent_request = transport.call_args.args[0]
        self.assertEqual(sent_request["args"]["body"], body)
        self.assertNotIn(str(path), json.dumps(sent_request))
        self.assertEqual(
            transport.call_args.kwargs["ssh_config"],
            "/tmp/agent-chat-ssh-config",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), response)

    def test_default_alias_is_portable(self):
        parser = restricted_client._parser()
        namespace = parser.parse_args(["ping"])
        self.assertEqual(namespace.ssh_alias, "agent-chat-restricted")


if __name__ == "__main__":
    unittest.main()
