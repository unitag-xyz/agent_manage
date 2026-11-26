"""Offline execution port; native connections and Manager leases are not prerequisites."""

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class LocalOptions:
    command: str
    executable: str | None
    project_dir: str | None
    dry_run: bool
    template_root: str | None
    config_path: str | None


class LocalExecution(Protocol):
    @property
    def restart_required(self) -> bool: ...
    def default_workspace(self, *, local: bool = False) -> str: ...
    def register_secret(self, value: str) -> None: ...
    def operation(self, name: str) -> Callable: ...
