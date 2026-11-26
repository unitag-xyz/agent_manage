"""Compatibility import for existing OpenClaw container launchers."""

from .adapters.openclaw.local import LocalRunner
from .process_result import CommandError, CommandResult

__all__ = ["LocalRunner", "CommandError", "CommandResult"]
