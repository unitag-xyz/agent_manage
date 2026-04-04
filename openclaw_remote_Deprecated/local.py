from __future__ import annotations

import json
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
    def __init__(
        self,
        openclaw_bin: str = "openclaw",
        project_dir: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        self.openclaw_bin = openclaw_bin
        self.project_dir = Path(project_dir).expanduser().resolve() if project_dir else None
        self.dry_run = dry_run

    def run(self, args: Sequence[str], timeout: Optional[float] = None) -> CommandResult:
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

        try:
            completed = subprocess.run(
                argv,
                text=True,
                capture_output=True,
                cwd=str(self.project_dir) if self.project_dir else None,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                argv=argv,
                command_text=command_text,
                returncode=124,
                stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + f"\nCommand timed out after {timeout} seconds",
                timed_out=True,
            )
            self._log(f"timeout after {timeout}s: {command_text}")
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
            if completed.stderr.strip():
                self._log(f"stderr: {completed.stderr.strip()}")
            if completed.stdout.strip():
                self._log(f"stdout: {completed.stdout.strip()}")
            raise CommandError(
                f"Command failed with exit code {completed.returncode}",
                result,
            )
        self._log(f"done ({completed.returncode}): {command_text}")
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
        self.log(message)

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
