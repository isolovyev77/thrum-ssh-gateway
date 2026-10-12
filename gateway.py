#!/usr/bin/env python3
"""Restricted forced-command gateway for Agent Chat."""

import argparse
import json
import math
import os
import pwd
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path


PROTOCOL_VERSION = 1
REMOTE_COMMAND = "agent-chat-v1"
MAX_REQUEST_BYTES = 64 * 1024
MAX_BODY_BYTES = 32 * 1024
INPUT_TIMEOUT_SECONDS = 10
RPC_TIMEOUT_SECONDS = 10
MAX_RPC_RESPONSE_BYTES = 8 * 1024 * 1024
RPC_PAGE_SIZE = 100
LIST_PAGE_SIZE = 10
_RPC_CWD_LOCK = threading.Lock()
OPERATIONS = {"ping", "inbox", "sent", "wait", "get", "read", "reply", "send"}
NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")
MESSAGE_ID_PATTERN = re.compile(r"^msg_[0-9A-Z]+$")
WAIT_TIMEOUT_PREFIX = "NO_MESSAGES_TIMEOUT "
WAIT_TIMEOUT_SUFFIX = " re-run thrum wait to continue listening"


class RequestError(Exception):
    pass


class AccessError(Exception):
    pass


class ServiceError(Exception):
    pass


class InputTimeout(Exception):
    pass


def _is_wait_timeout_stderr(value):
    value = value.strip()
    if not value:
        return True
    if not value.startswith(WAIT_TIMEOUT_PREFIX) or not value.endswith(
        WAIT_TIMEOUT_SUFFIX
    ):
        return False
    separator = value[len(WAIT_TIMEOUT_PREFIX) : -len(WAIT_TIMEOUT_SUFFIX)]
    return len(separator) == 1 and ord(separator) == 0x2014


def _response(ok, data=None, error=None):
    return {"ok": ok, "data": data, "error": error}


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RequestError()
        result[key] = value
    return result


def _require_exact_args(args, required=(), optional=()):
    if not isinstance(args, dict):
        raise RequestError()
    keys = set(args)
    required_keys = set(required)
    if not required_keys.issubset(keys):
        raise RequestError()
    if not keys.issubset(required_keys | set(optional)):
        raise RequestError()


def _validate_name(value):
    if not isinstance(value, str) or NAME_PATTERN.fullmatch(value) is None:
        raise RequestError()


def _validate_message_id(value):
    if not isinstance(value, str) or MESSAGE_ID_PATTERN.fullmatch(value) is None:
        raise RequestError()


def _validate_body(value):
    if not isinstance(value, str):
        raise RequestError()
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RequestError() from exc
    if len(encoded) > MAX_BODY_BYTES:
        raise RequestError()


def _validate_request(request):
    if not isinstance(request, dict) or set(request) != {"version", "op", "args"}:
        raise RequestError()
    if type(request["version"]) is not int or request["version"] != PROTOCOL_VERSION:
        raise RequestError()
    operation = request["op"]
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise RequestError()
    args = request["args"]

    if operation in {"ping", "sent"}:
        _require_exact_args(args)
    elif operation == "inbox":
        _require_exact_args(args, optional=("unread",))
        if "unread" in args and not isinstance(args["unread"], bool):
            raise RequestError()
    elif operation == "wait":
        _require_exact_args(args, required=("timeout",))
        timeout = args["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise RequestError()
        if not 1 <= timeout <= 50:
            raise RequestError()
    elif operation in {"get", "read"}:
        _require_exact_args(args, required=("message_id",))
        _validate_message_id(args["message_id"])
    elif operation == "reply":
        _require_exact_args(args, required=("message_id", "body"))
        _validate_message_id(args["message_id"])
        _validate_body(args["body"])
    elif operation == "send":
        _require_exact_args(args, required=("to", "body"))
        _validate_name(args["to"])
        _validate_body(args["body"])


def _server_environment(identity):
    home = pwd.getpwuid(os.getuid()).pw_dir
    return {
        "HOME": home,
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "THRUM_NAME": identity,
        "THRUM_NO_HINTS": "1",
    }


def _run_thrum(
    binary,
    hub,
    identity,
    arguments,
    body=None,
    runner=subprocess.run,
    allowed_returncodes=(0,),
    empty_returncodes=(),
    timeout=15,
):
    try:
        completed = runner(
            [binary] + list(arguments),
            cwd=hub,
            env=_server_environment(identity),
            input=body,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError() from exc
    if completed.returncode not in allowed_returncodes:
        raise ServiceError()
    stdout = completed.stdout.strip()
    if not stdout:
        if (
            completed.returncode in empty_returncodes
            and _is_wait_timeout_stderr(completed.stderr)
        ):
            return None
        raise ServiceError()
    try:
        result = json.loads(stdout)
    except (TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise ServiceError() from exc
    if not isinstance(result, dict):
        raise ServiceError()
    return result


def _message_items(data):
    if not isinstance(data, dict) or "messages" not in data:
        raise ServiceError()
    items = data["messages"]
    if not isinstance(items, list):
        raise ServiceError()
    for item in items:
        if not isinstance(item, dict):
            raise ServiceError()
        message_id = item.get("message_id")
        if not isinstance(message_id, str) or MESSAGE_ID_PATTERN.fullmatch(message_id) is None:
            raise ServiceError()
    return items


def _strict_rpc_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ServiceError()
        result[key] = value
    return result


def _reject_rpc_constant(_value):
    raise ServiceError()


def _effective_thrum_dir(hub):
    try:
        hub_root = Path(hub).resolve(strict=True)
        local = hub_root / ".thrum"
        if local.is_symlink() or not local.is_dir():
            raise ServiceError()
        redirect = local / "redirect"
        if not os.path.lexists(str(redirect)):
            return local
        if redirect.is_symlink() or not redirect.is_file():
            raise ServiceError()
        raw_target = redirect.read_bytes()
        if len(raw_target) > 4096:
            raise ServiceError()
        target_text = raw_target.decode("utf-8").strip()
        target = Path(target_text)
        if not target.is_absolute() or target.is_symlink():
            raise ServiceError()
        target = target.resolve(strict=True)
        if not target.is_dir() or os.path.lexists(str(target / "redirect")):
            raise ServiceError()
        return target
    except (OSError, UnicodeError, ValueError) as exc:
        raise ServiceError() from exc


def _fixed_file(directory, *parts):
    try:
        candidate = directory.joinpath(*parts)
        if candidate.is_symlink() or not candidate.is_file():
            raise ServiceError()
        resolved = candidate.resolve(strict=True)
        if os.path.commonpath((str(directory), str(resolved))) != str(directory):
            raise ServiceError()
        return resolved
    except (OSError, ValueError) as exc:
        raise ServiceError() from exc


def _identity_role(identity, hub):
    identity_path = _fixed_file(
        _effective_thrum_dir(hub), "identities", identity + ".json"
    )
    try:
        raw = identity_path.read_bytes()
        if len(raw) > MAX_REQUEST_BYTES:
            raise ServiceError()
        record = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_rpc_object,
            parse_constant=_reject_rpc_constant,
        )
        agent = record["agent"]
        if not isinstance(agent, dict) or agent.get("Name") != identity:
            raise ServiceError()
        role = agent.get("Role")
        if not isinstance(role, str) or NAME_PATTERN.fullmatch(role) is None:
            raise ServiceError()
        return role
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ServiceError() from exc


def _rpc_call(hub, method, params):
    thrum_dir = _effective_thrum_dir(hub)
    working_dir = Path(hub).resolve(strict=True)
    socket_path = thrum_dir / "var" / "thrum.sock"
    try:
        socket_stat = socket_path.lstat()
        if stat.S_ISLNK(socket_stat.st_mode) or not stat.S_ISSOCK(socket_stat.st_mode):
            raise ServiceError()
        resolved_socket = socket_path.resolve(strict=True)
        if os.path.commonpath((str(thrum_dir), str(resolved_socket))) != str(thrum_dir):
            raise ServiceError()
        request = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        with _RPC_CWD_LOCK:
            previous_directory = os.open(".", os.O_RDONLY)
            try:
                os.chdir(str(working_dir))
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(RPC_TIMEOUT_SECONDS)
                    connection.connect(str(resolved_socket))
                    connection.sendall(request)
                    received = bytearray()
                    while b"\n" not in received:
                        chunk = connection.recv(65536)
                        if not chunk:
                            raise ServiceError()
                        received.extend(chunk)
                        if len(received) > MAX_RPC_RESPONSE_BYTES:
                            raise ServiceError()
            finally:
                os.fchdir(previous_directory)
                os.close(previous_directory)
    except (OSError, socket.timeout) as exc:
        raise ServiceError() from exc
    line, separator, trailing = bytes(received).partition(b"\n")
    if not separator or trailing.strip():
        raise ServiceError()
    try:
        response = json.loads(
            line.decode("utf-8"),
            object_pairs_hook=_strict_rpc_object,
            parse_constant=_reject_rpc_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ServiceError() from exc
    if (
        not isinstance(response, dict)
        or response.get("jsonrpc") != "2.0"
        or type(response.get("id")) is not int
        or response["id"] != 1
        or (("result" in response) == ("error" in response))
        or "error" in response
        or not isinstance(response.get("result"), dict)
    ):
        raise ServiceError()
    return response["result"]


def _validate_message_page(data, requested_page, requested_page_size):
    messages = _message_items(data)
    for key in ("total", "page", "page_size", "total_pages"):
        if type(data.get(key)) is not int or data[key] < 0:
            raise ServiceError()
    if data["page"] != requested_page:
        raise ServiceError()
    if not 1 <= data["page_size"] <= requested_page_size:
        raise ServiceError()
    if data["total_pages"] == 0 and (data["total"] != 0 or messages):
        raise ServiceError()
    if data["total_pages"] and requested_page > data["total_pages"]:
        raise ServiceError()
    return data


def _inbox_params(identity, role, unread, page_size, page, stable=False):
    params = {
        "exclude_self": True,
        "caller_agent_id": identity,
        "caller_mention_role": role,
        "for_agent": identity,
        "for_agent_role": role,
        "unread": unread,
        "page_size": page_size,
        "page": page,
    }
    if stable:
        params.update({"sort_by": "created_at", "sort_order": "asc"})
    return params


def _inbox(identity, binary, hub, runner, unread=False):
    del binary, runner
    role = _identity_role(identity, hub)
    data = _rpc_call(
        hub,
        "message.list",
        _inbox_params(identity, role, unread, LIST_PAGE_SIZE, 1),
    )
    return _validate_message_page(data, 1, LIST_PAGE_SIZE)


def _sent(identity, binary, hub, runner):
    del binary, runner
    data = _rpc_call(
        hub,
        "message.outbox",
        {"caller_agent_id": identity, "page_size": LIST_PAGE_SIZE, "page": 1},
    )
    return _validate_message_page(data, 1, LIST_PAGE_SIZE)


def _get_message(message_id, hub):
    data = _rpc_call(hub, "message.get", {"message_id": message_id})
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict) or message.get("message_id") != message_id:
        raise ServiceError()
    return data


def _contains_message(data, message_id):
    return any(item["message_id"] == message_id for item in _message_items(data))


def _require_inbox_membership(message_id, identity, binary, hub, runner):
    del binary, runner
    role = _identity_role(identity, hub)
    total_pages = None
    total_messages = None
    page = 1
    while total_pages is None or page <= total_pages:
        data = _rpc_call(
            hub,
            "message.list",
            _inbox_params(identity, role, False, RPC_PAGE_SIZE, page, stable=True),
        )
        data = _validate_message_page(data, page, RPC_PAGE_SIZE)
        if total_pages is None:
            total_pages = data["total_pages"]
            total_messages = data["total"]
        elif data["total_pages"] != total_pages or data["total"] != total_messages:
            raise ServiceError()
        if _contains_message(data, message_id):
            return
        if page < total_pages and not data["messages"]:
            raise ServiceError()
        page += 1
    raise AccessError()


def _require_get_membership(message_id, identity, binary, hub, runner):
    try:
        _require_inbox_membership(message_id, identity, binary, hub, runner)
        return
    except AccessError:
        pass
    total_pages = None
    total_messages = None
    page = 1
    while total_pages is None or page <= total_pages:
        data = _rpc_call(
            hub,
            "message.outbox",
            {"caller_agent_id": identity, "page_size": RPC_PAGE_SIZE, "page": page},
        )
        data = _validate_message_page(data, page, RPC_PAGE_SIZE)
        if total_pages is None:
            total_pages = data["total_pages"]
            total_messages = data["total"]
        elif data["total_pages"] != total_pages or data["total"] != total_messages:
            raise ServiceError()
        if _contains_message(data, message_id):
            return
        if page < total_pages and not data["messages"]:
            raise ServiceError()
        page += 1
    raise AccessError()


def _wait(identity, args, binary, hub, runner):
    timeout = args["timeout"]
    deadline = time.monotonic() + timeout
    initial = _inbox(identity, binary, hub, runner, unread=True)
    messages = _message_items(initial)
    if messages:
        return {"status": "received", "messages": messages}

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "timeout", "messages": []}
        duration = "%dms" % max(1, math.ceil(remaining * 1000))
        _run_thrum(
            binary,
            hub,
            identity,
            ["wait", "--timeout", duration, "--json"],
            runner=runner,
            allowed_returncodes=(0, 1),
            empty_returncodes=(1,),
            timeout=remaining + 1,
        )
        final = _inbox(identity, binary, hub, runner, unread=True)
        messages = _message_items(final)
        if messages:
            return {"status": "received", "messages": messages}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "timeout", "messages": []}
        time.sleep(min(0.05, remaining))


def _dispatch_validated(request, identity, allow_to, binary, hub, runner):
    operation = request["op"]
    args = request["args"]
    if operation == "ping":
        return {"protocol": PROTOCOL_VERSION, "identity": identity}
    if operation == "wait":
        return _wait(identity, args, binary, hub, runner)
    if operation == "inbox":
        data = _inbox(identity, binary, hub, runner, unread=args.get("unread", False))
        _message_items(data)
        return data
    if operation == "sent":
        data = _sent(identity, binary, hub, runner)
        _message_items(data)
        return data
    if operation == "send":
        if args["to"] not in allow_to:
            raise AccessError()
        return _run_thrum(
            binary,
            hub,
            identity,
            [
                "send",
                "--to",
                "@" + args["to"],
                "--format",
                "plain",
                "--stdin",
                "--json",
            ],
            body=args["body"],
            runner=runner,
        )

    message_id = args["message_id"]
    if operation == "get":
        _require_get_membership(message_id, identity, binary, hub, runner)
        return _get_message(message_id, hub)
    else:
        _require_inbox_membership(message_id, identity, binary, hub, runner)
        body = args.get("body")
        if operation == "read":
            arguments = ["message", "read", message_id, "--json"]
        else:
            arguments = [
                "reply",
                message_id,
                "--format",
                "plain",
                "--stdin",
                "--json",
            ]
    return _run_thrum(
        binary, hub, identity, arguments, body=body, runner=runner
    )


def dispatch(request, identity, allow_to, binary, hub, runner=subprocess.run):
    """Validate and execute one request under a server-owned identity."""
    try:
        _validate_name(identity)
        configured_targets = frozenset(allow_to)
        for target in configured_targets:
            _validate_name(target)
        if not os.path.isabs(binary) or not os.path.isabs(hub):
            raise ServiceError()
        _validate_request(request)
        data = _dispatch_validated(
            request, identity, configured_targets, binary, hub, runner
        )
        return _response(True, data=data)
    except RequestError:
        return _response(False, error="invalid request")
    except AccessError:
        return _response(False, error="access denied")
    except ServiceError:
        return _response(False, error="service unavailable")
    except Exception:
        return _response(False, error="service unavailable")


def handle_request_bytes(
    raw,
    original_command,
    identity,
    allow_to,
    binary,
    hub,
    runner=subprocess.run,
):
    """Apply the SSH command and byte-size boundaries before JSON dispatch."""
    if original_command != REMOTE_COMMAND:
        return _response(False, error="command rejected")
    if not isinstance(raw, bytes):
        return _response(False, error="invalid request")
    if len(raw) > MAX_REQUEST_BYTES:
        return _response(False, error="request too large")
    try:
        request = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, RequestError):
        return _response(False, error="invalid request")
    return dispatch(request, identity, allow_to, binary, hub, runner=runner)


def _parser():
    parser = argparse.ArgumentParser(description="Restricted Agent Chat gateway")
    parser.add_argument("--identity", required=True)
    parser.add_argument("--allow-to", action="append", default=[])
    parser.add_argument("--binary", required=True)
    parser.add_argument("--hub", required=True)
    return parser


def _read_stdin_bytes():
    source = getattr(sys.stdin, "buffer", sys.stdin)
    previous_handler = signal.getsignal(signal.SIGALRM)

    def deadline_reached(_signum, _frame):
        raise InputTimeout()

    signal.signal(signal.SIGALRM, deadline_reached)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, INPUT_TIMEOUT_SECONDS)
    try:
        raw = source.read(MAX_REQUEST_BYTES + 1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
        signal.signal(signal.SIGALRM, previous_handler)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return raw


def main(argv=None):
    namespace = _parser().parse_args(argv)
    if os.environ.get("SSH_TTY"):
        response = _response(False, error="command rejected")
    else:
        original_command = os.environ.get("SSH_ORIGINAL_COMMAND")
        if original_command != REMOTE_COMMAND:
            response = _response(False, error="command rejected")
        else:
            try:
                raw = _read_stdin_bytes()
            except InputTimeout:
                response = _response(False, error="invalid request")
            else:
                response = handle_request_bytes(
                    raw,
                    original_command=original_command,
                    identity=namespace.identity,
                    allow_to=namespace.allow_to,
                    binary=namespace.binary,
                    hub=namespace.hub,
                )
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 1 if response["error"] == "command rejected" else 0


if __name__ == "__main__":
    raise SystemExit(main())
