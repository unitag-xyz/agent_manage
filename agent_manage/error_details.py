"""Shared error details for workflow failures and the CLI response envelope."""

from __future__ import annotations

import json

from .local import CommandError


def embedded_error_payload(exc: Exception) -> dict:
    try:
        payload = json.loads(str(exc))
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def command_error_details(exc: Exception) -> dict:
    if not isinstance(exc, CommandError):
        return {}
    return {"command": exc.result.command_text, "returncode": exc.result.returncode,
            "stdout": exc.result.stdout, "stderr": exc.result.stderr,
            "timed_out": exc.result.timed_out, **getattr(exc, "hook_details", {})}
