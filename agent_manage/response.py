from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any, Dict

from .local import CommandError

TYPE_CODE_SUCCESS = 1
TYPE_CODE_ACCEPTED = 2
TYPE_CODE_NOT_FOUND = 10
TYPE_CODE_INVALID_ARGUMENT = 11
TYPE_CODE_CONFLICT = 12
TYPE_CODE_COMMAND_FAILED = 20
TYPE_CODE_OPERATION_ROLLED_BACK = 21
TYPE_CODE_INTERNAL_ERROR = 50


class CliArgumentError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliArgumentError(message)


def build_success_response(
    result: object,
    *,
    message: str = "OK",
    type_code: int = TYPE_CODE_SUCCESS,
) -> Dict[str, object]:
    return {
        "result": result,
        "error": None,
        "typeCode": type_code,
        "message": message,
        "serverTimeStamp": _server_timestamp(),
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
        details = {
            "command": exc.result.command_text,
            "returncode": exc.result.returncode,
            "stdout": exc.result.stdout,
            "stderr": exc.result.stderr,
            "timed_out": exc.result.timed_out,
        }
    if details:
        error["details"] = details

    steps = payload.get("steps") if payload else None
    if steps:
        error["steps"] = steps

    rollback = payload.get("rollback") if payload else None
    if rollback:
        error["rollback"] = rollback

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
    }


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _server_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _embedded_error_payload(exc: Exception) -> Dict[str, Any]:
    try:
        payload = json.loads(str(exc))
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _type_code_for_exception(exc: Exception, payload: Dict[str, Any]) -> int:
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
        return "COMMAND_EXECUTION_FAILED"
    if payload:
        if payload.get("rollback"):
            return "OPERATION_FAILED_WITH_ROLLBACK"
        details = payload.get("details") or {}
        if isinstance(details, dict) and details.get("returncode") is not None:
            return "COMMAND_EXECUTION_FAILED"
    return "INTERNAL_ERROR"
