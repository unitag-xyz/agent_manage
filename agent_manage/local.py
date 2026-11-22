from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from json import JSONDecoder
from pathlib import Path
from typing import List, Optional, Sequence


@dataclass
class CommandResult:
    argv: List[str]
    command_text: str
    returncode: int
    stdout: str
    stderr: str
    skipped: bool = False
    timed_out: bool = False


class CommandError(RuntimeError):
    def __init__(self, message: str, result: CommandResult) -> None:
        super().__init__(message)
        self.result = result


class LocalRunner:
    COMMAND_OUTPUT_TAIL_BYTES = 16 * 1024
    def __init__(
        self,
        openclaw_bin: str = "openclaw",
        project_dir: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        self.openclaw_bin = openclaw_bin
        self.project_dir = Path(project_dir).expanduser().resolve() if project_dir else None
        self.dry_run = dry_run
        self._redaction_values: List[str] = []

    def add_redaction_value(self, value: str) -> None:
        if value and value not in self._redaction_values:
            self._redaction_values.append(value)

    def run(
        self,
        args: Sequence[str],
        timeout: Optional[float] = None,
        stream_output: bool = False,
        env_overrides: Optional[dict[str, str]] = None,
    ) -> CommandResult:
        argv = list(args)
        command_text = " ".join(shlex.quote(part) for part in argv)
        self._log(f"run: {command_text}")
        if self.dry_run:
            self._log("dry-run: skipped")
            return CommandResult(
                argv=argv,
                command_text=command_text,
                returncode=0,
                stdout="",
                stderr="",
                skipped=True,
            )

        env = self._command_env()
        if env_overrides:
            env.update(env_overrides)
        if stream_output:
            return self._run_streaming(argv, command_text, env)

        try:
            completed = subprocess.run(
                argv,
                text=True,
                capture_output=True,
                cwd=str(self.project_dir) if self.project_dir else None,
                env=env,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = self._as_text(exc.stdout)
            stderr = self._as_text(exc.stderr)
            result = CommandResult(
                argv=argv,
                command_text=command_text,
                returncode=124,
                stdout=stdout,
                stderr=stderr + f"\nCommand timed out after {timeout} seconds",
                timed_out=True,
            )
            self._log(f"timeout after {timeout}s: {command_text}")
            self._log_command_output_tail("stderr", result.stderr)
            self._log_command_output_tail("stdout", result.stdout)
            raise CommandError(
                f"Command timed out after {timeout} seconds",
                result,
            ) from exc
        result = CommandResult(
            argv=argv,
            command_text=command_text,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if completed.returncode != 0:
            self._log(f"failed ({completed.returncode}): {command_text}")
            self._log_command_output_tail("stderr", completed.stderr)
            self._log_command_output_tail("stdout", completed.stdout)
            raise CommandError(
                f"Command failed with exit code {completed.returncode}",
                result,
            )
        self._log(f"done ({completed.returncode}): {command_text}")
        return result

    def _run_streaming(self, argv: List[str], command_text: str, env: dict[str, str]) -> CommandResult:
        process = subprocess.Popen(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(self.project_dir) if self.project_dir else None,
            env=env,
            bufsize=1,
        )
        output_lines: List[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            output_lines.append(line)
        process.stdout.close()
        returncode = process.wait()
        stdout = "".join(output_lines)
        result = CommandResult(
            argv=argv,
            command_text=command_text,
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )
        if returncode != 0:
            self._log(f"failed ({returncode}): {command_text}")
            self._log_command_output_tail("stdout", stdout)
            raise CommandError(
                f"Command failed with exit code {returncode}",
                result,
            )
        for line in output_lines:
            self._log(f"output: {line.rstrip()}")
        self._log(f"done ({returncode}): {command_text}")
        return result

    def run_json(self, args: Sequence[str], timeout: Optional[float] = None):
        result = self.run(args, timeout=timeout)
        if result.skipped:
            return {"skipped": True, "command": result.command_text}
        if not result.stdout.strip():
            return {}
        return self._extract_json(result.stdout)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[agentctl {timestamp}] {message}", file=sys.stderr, flush=True)

    def _log(self, message: str) -> None:
        self.log(self._redact(message))

    def _log_command_output_tail(self, stream_name: str, value: str) -> None:
        if not value:
            return
        raw = value.encode("utf-8", errors="replace")
        tail = raw[-self.COMMAND_OUTPUT_TAIL_BYTES :]
        tail_text = tail.decode("utf-8", errors="replace")
        truncated = len(raw) > len(tail)
        self._log(
            f"{stream_name}: utf8_bytes={len(raw)} truncated={str(truncated).lower()} "
            f"tail={tail_text}"
        )

    def _redact(self, value: str) -> str:
        for secret in self._redaction_values:
            value = value.replace(secret, "[REDACTED]")
        return value

    @staticmethod
    def _as_text(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    def _command_env(self) -> dict[str, str]:
        env = os.environ.copy()
        from .core.runtime import CURRENT_RUNTIME
        context = CURRENT_RUNTIME.get()
        if context is not None:
            if not context.inherit_process_env:
                env = {key: value for key, value in env.items() if key in {
                    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
                    "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}}
            # A profile inherited from the manager's shell must not select another runtime.
            for key in ("OPENCLAW_PROFILE", "OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR"):
                env.pop(key, None)
            env.update(context.environment())
        home = env.get("HOME") or str(Path.home())
        runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        dbus_address = env.get("DBUS_SESSION_BUS_ADDRESS") or f"unix:path={runtime_dir}/bus"

        env["HOME"] = home
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address

        npm_bin = str(Path(home) / ".npm-global" / "bin")
        path_parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
        required_paths = [npm_bin, "/usr/local/sbin", "/usr/sbin", "/sbin"]
        missing_paths = [path for path in required_paths if path not in path_parts]
        if missing_paths:
            path_parts = [*missing_paths, *path_parts]
            env["PATH"] = os.pathsep.join(path_parts)
            self._log(f"env: prepended PATH with {os.pathsep.join(missing_paths)}")

        self._log(
            "env: "
            f"HOME={env['HOME']} "
            f"XDG_RUNTIME_DIR={env['XDG_RUNTIME_DIR']} "
            f"DBUS_SESSION_BUS_ADDRESS={env['DBUS_SESSION_BUS_ADDRESS']}"
        )
        return env

    def _extract_json(self, text: str):
        decoder = JSONDecoder()
        for index, char in enumerate(text):
            if char not in "[{":
                continue
            try:
                value, end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            trailing = text[index + end :].strip()
            if trailing:
                self._log(f"json: ignored trailing output: {trailing[:200]}")
            return value
        raise json.JSONDecodeError("Could not find JSON object in command output", text, 0)
