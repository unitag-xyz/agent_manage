"""Transport-neutral result and error for local subprocesses."""

from dataclasses import dataclass


@dataclass
class CommandResult:
    argv: list[str]
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
