#!/usr/bin/env python3
"""Portable client for the restricted Agent Chat gateway."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


PROTOCOL_VERSION = 1
MAX_BODY_BYTES = 32 * 1024
DEFAULT_SSH_ALIAS = "agent-chat-restricted"
REMOTE_COMMAND = "agent-chat-v1"
OPERATIONS = {"ping", "inbox", "sent", "wait", "get", "read", "reply", "send"}
NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")
MESSAGE_ID_PATTERN = re.compile(r"^msg_[0-9A-Z]+$")
SSH_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _require_exact_args(args, required=(), optional=()):
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    keys = set(args)
    required_keys = set(required)
    if not required_keys.issubset(keys):
        raise ValueError("invalid argument set")
    if not keys.issubset(required_keys | set(optional)):
        raise ValueError("invalid argument set")


def _validate_name(value):
    if not isinstance(value, str) or NAME_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid recipient")


def _validate_message_id(value):
    if not isinstance(value, str) or MESSAGE_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid message_id")


def _validate_body(value):
    if not isinstance(value, str):
        raise ValueError("body must be a string")
    if len(value.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("body is too large")


def _validate_operation_args(operation, args):
    if operation in {"ping", "sent"}:
        _require_exact_args(args)
    elif operation == "inbox":
        _require_exact_args(args, optional=("unread",))
        if "unread" in args and not isinstance(args["unread"], bool):
            raise ValueError("unread must be boolean")
    elif operation == "wait":
        _require_exact_args(args, required=("timeout",))
        timeout = args["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError("invalid timeout")
        if not 1 <= timeout <= 50:
            raise ValueError("invalid timeout")
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


def build_request(operation, args):
    """Build a request without any client-selected identity."""
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValueError("unknown operation")
    _validate_operation_args(operation, args)
    return {"version": PROTOCOL_VERSION, "op": operation, "args": dict(args)}


def _validate_alias(value):
    if not isinstance(value, str) or SSH_ALIAS_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid SSH alias")


def _validate_ssh_config(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid SSH config path")


def _validate_response(response):
    if not isinstance(response, dict) or set(response) != {"ok", "data", "error"}:
        raise RuntimeError("invalid response")
    if not isinstance(response["ok"], bool):
        raise RuntimeError("invalid response")
    if response["ok"]:
        if not isinstance(response["data"], dict) or response["error"] is not None:
            raise RuntimeError("invalid response")
    elif response["data"] is not None or not isinstance(response["error"], str):
        raise RuntimeError("invalid response")


def _validate_request(request):
    if not isinstance(request, dict) or set(request) != {"version", "op", "args"}:
        raise ValueError("invalid request")
    if type(request["version"]) is not int or request["version"] != PROTOCOL_VERSION:
        raise ValueError("invalid request")
    try:
        expected = build_request(request["op"], request["args"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid request") from exc
    if request != expected:
        raise ValueError("invalid request")


def transport(
    request,
    ssh_alias=DEFAULT_SSH_ALIAS,
    ssh_config=None,
    runner=subprocess.run,
):
    """Send exactly one request through a configured SSH alias."""
    _validate_alias(ssh_alias)
    if ssh_config is not None:
        _validate_ssh_config(ssh_config)
    _validate_request(request)
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    argv = ["ssh"]
    if ssh_config is not None:
        argv.extend(["-F", ssh_config])
    argv.extend([
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        ssh_alias,
        REMOTE_COMMAND,
    ])
    try:
        completed = runner(
            argv,
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            timeout=65,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("transport failed") from exc
    if completed.returncode != 0:
        raise RuntimeError("transport failed")
    try:
        response = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError("invalid response") from exc
    _validate_response(response)
    return response


def _read_body(path):
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _parser():
    parser = argparse.ArgumentParser(description="Restricted Agent Chat client")
    parser.add_argument("--ssh-alias", default=DEFAULT_SSH_ALIAS)
    parser.add_argument("--ssh-config")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    subparsers.add_parser("ping")
    inbox = subparsers.add_parser("inbox")
    inbox.add_argument("--unread", action="store_true")
    subparsers.add_parser("sent")
    wait = subparsers.add_parser("wait")
    wait.add_argument("--timeout", type=int, required=True)
    get = subparsers.add_parser("get")
    get.add_argument("message_id")
    read = subparsers.add_parser("read")
    read.add_argument("message_id")
    reply = subparsers.add_parser("reply")
    reply.add_argument("message_id")
    reply.add_argument("--body-file", required=True)
    send = subparsers.add_parser("send")
    send.add_argument("--to", required=True)
    send.add_argument("--body-file", required=True)
    return parser


def _request_args(namespace):
    if namespace.operation == "inbox":
        return {"unread": True} if namespace.unread else {}
    if namespace.operation == "wait":
        return {"timeout": namespace.timeout}
    if namespace.operation in {"get", "read"}:
        return {"message_id": namespace.message_id}
    if namespace.operation == "reply":
        return {
            "message_id": namespace.message_id,
            "body": _read_body(namespace.body_file),
        }
    if namespace.operation == "send":
        return {"to": namespace.to, "body": _read_body(namespace.body_file)}
    return {}


def main(argv=None):
    namespace = _parser().parse_args(argv)
    try:
        request = build_request(namespace.operation, _request_args(namespace))
        response = transport(
            request,
            ssh_alias=namespace.ssh_alias,
            ssh_config=namespace.ssh_config,
        )
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        response = {"ok": False, "data": None, "error": str(exc)}
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
