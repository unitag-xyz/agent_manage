from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, Dict

from .local import CommandError
from .error_details import command_error_details, embedded_error_payload as _embedded_error_payload
from .segment_timing import CURRENT

TYPE_CODE_SUCCESS = 1
TYPE_CODE_ACCEPTED = 2
TYPE_CODE_NOT_FOUND = 10
TYPE_CODE_INVALID_ARGUMENT = 11
TYPE_CODE_CONFLICT = 12
TYPE_CODE_COMMAND_FAILED = 20
TYPE_CODE_OPERATION_ROLLED_BACK = 21
TYPE_CODE_INTERNAL_ERROR = 50
TYPE_CODE_OUTPUT_TOO_LARGE = 20
# A 5 MiB media chunk needs about 6.67 MiB after base64 plus its JSON envelope.
MAX_PROTOCOL_RESPONSE_BYTES = 8 * 1024 * 1024


class CliArgumentError(ValueError):
    pass


class ProtocolError(Exception):
    """A domain error whose stable code is part of the public CLI contract."""

    def __init__(self, code: str, message: str, type_code: int = TYPE_CODE_INVALID_ARGUMENT):
        super().__init__(message)
        self.code = code
        self.type_code = type_code


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliArgumentError(message)


def build_success_response(
    result: object,
    *,
    message: str = "OK",
    type_code: int = TYPE_CODE_SUCCESS,
    restart_required: bool = False,
) -> Dict[str, object]:
    return {
        "result": result,
        "error": None,
        "typeCode": type_code,
        "message": message,
        "serverTimeStamp": _server_timestamp(),
        "restartRequired": restart_required,
    }


def build_error_response(exc: Exception) -> Dict[str, object]:
    payload = _embedded_error_payload(exc)
    message = payload.get("error") if payload else str(exc)
    type_code = _type_code_for_exception(exc, payload)
    error: Dict[str, object] = {
        "code": _error_code_for_exception(exc, payload),
    }

    details = payload.get("details") if payload else None
    if not details and isinstance(exc, CommandError):
        details = command_error_details(exc)
    if details:
        error["details"] = details

    steps = payload.get("steps") if payload else None
    if steps:
        error["steps"] = steps

    rollback = payload.get("rollback") if payload else None
    if rollback:
        error["rollback"] = rollback

    total_elapsed_ms = payload.get("total_elapsed_ms") if payload else None
    if isinstance(total_elapsed_ms, (int, float)):
        error["total_elapsed_ms"] = total_elapsed_ms

    if isinstance(exc, CliArgumentError):
        error["details"] = {
            "kind": "argument",
            "reason": str(exc),
        }

    return {
        "result": None,
        "error": error,
        "typeCode": type_code,
        "message": message,
        "serverTimeStamp": _server_timestamp(),
        "restartRequired": False,
    }


def redact_sensitive_values(value: object, secrets: list[str]) -> object:
    """Remove command-supplied secrets from error output without changing success data."""
    values = [secret for secret in secrets if secret]
    if isinstance(value, str):
        for secret in values:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact_sensitive_values(item, values) for item in value]
    if isinstance(value, dict):
        return {key: redact_sensitive_values(item, values) for key, item in value.items()}
    return value


def print_json(value: object) -> None:
    timing = CURRENT.get()
    if timing is not None:
        # Measure one normal serialization before finalizing diagnostic metadata.
        # The final metadata serialization and stdout delivery cannot measure
        # themselves inside the same response.
        with timing.span("responseSerializationMs"):
            serialized = json.dumps(value, indent=2, ensure_ascii=False)
            payload_bytes = len(serialized.encode("utf-8"))
        timing.counts["responseBytesBeforeTimingFinalization"] = payload_bytes
        value = dict(value)
        if isinstance(value.get("result"), dict):
            value["result"] = {**value["result"], "timings": timing.snapshot()}
        else:
            value["timings"] = timing.snapshot()
    serialized = json.dumps(value, indent=2, ensure_ascii=False)
    original_utf8_bytes = len(serialized.encode("utf-8"))
    budget_applied = original_utf8_bytes > MAX_PROTOCOL_RESPONSE_BYTES
    if budget_applied:
        serialized = json.dumps(
            {
                "result": None,
                "error": {
                    "code": "RESPONSE_OUTPUT_TOO_LARGE",
                    "details": {"totalUtf8Bytes": original_utf8_bytes},
                },
                "typeCode": TYPE_CODE_OUTPUT_TOO_LARGE,
                "message": "AgentManager response exceeds the 8 MiB output budget",
                "serverTimeStamp": _server_timestamp(),
                "restartRequired": False,
                **({"timings": timing.snapshot()} if timing is not None else {}),
            },
            indent=2,
            ensure_ascii=False,
        )
    emitted_utf8_bytes = len(serialized.encode("utf-8"))
    print(
        "[agentctl response] "
        f"original_utf8_bytes={original_utf8_bytes} "
        f"emitted_utf8_bytes={emitted_utf8_bytes} "
        f"budget_applied={str(budget_applied).lower()}",
        file=sys.stderr,
    )
    print(serialized)


def _server_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _type_code_for_exception(exc: Exception, payload: Dict[str, Any]) -> int:
    if isinstance(exc, ProtocolError):
        return exc.type_code
    if isinstance(exc, FileNotFoundError):
        return TYPE_CODE_NOT_FOUND
    if isinstance(exc, (CliArgumentError, ValueError)):
        return TYPE_CODE_INVALID_ARGUMENT
    if isinstance(exc, FileExistsError):
        return TYPE_CODE_CONFLICT
    if isinstance(exc, CommandError):
        return TYPE_CODE_COMMAND_FAILED
    if payload:
        if payload.get("rollback"):
            return TYPE_CODE_OPERATION_ROLLED_BACK
        details = payload.get("details") or {}
        if isinstance(details, dict) and details.get("returncode") is not None:
            return TYPE_CODE_COMMAND_FAILED
    return TYPE_CODE_INTERNAL_ERROR


def _error_code_for_exception(exc: Exception, payload: Dict[str, Any]) -> str:
    if isinstance(exc, ProtocolError):
        return exc.code
    message = (payload.get("error") if payload else None) or str(exc)
    if isinstance(exc, FileNotFoundError):
        if message.startswith("Template archive not found:"):
            return "TEMPLATE_ARCHIVE_NOT_FOUND"
        if message.startswith("Config file not found:"):
            return "CONFIG_FILE_NOT_FOUND"
        if message.startswith("Agent not found:") or message.startswith("Agent '"):
            return "AGENT_NOT_FOUND"
        if message.startswith("Telegram account '"):
            return "TELEGRAM_ACCOUNT_NOT_FOUND"
        if message.startswith("Weixin account '"):
            return "WEIXIN_ACCOUNT_NOT_FOUND"
        return "ENTITY_NOT_FOUND"
    if isinstance(exc, FileExistsError):
        if message.startswith("Agent already exists:"):
            return "AGENT_ALREADY_EXISTS"
        if message.startswith("Workspace already exists and is not empty:"):
            return "WORKSPACE_NOT_EMPTY"
        return "STATE_CONFLICT"
    if isinstance(exc, CliArgumentError):
        return "INVALID_ARGUMENT"
    if isinstance(exc, ValueError):
        return "VALIDATION_ERROR"
    if isinstance(exc, CommandError):
        return getattr(exc, "error_code", "COMMAND_EXECUTION_FAILED")
    if payload:
        if payload.get("rollback"):
            return "OPERATION_FAILED_WITH_ROLLBACK"
        details = payload.get("details") or {}
        if isinstance(details, dict) and details.get("returncode") is not None:
            return "COMMAND_EXECUTION_FAILED"
    return "INTERNAL_ERROR"
