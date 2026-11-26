"""Explicit runtime scope; no per-request mutation of process environment."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

CURRENT_RUNTIME = ContextVar("agent_manager_runtime", default=None)
CURRENT_WRITE_GUARD = ContextVar("agent_manager_write_guard", default=None)


@dataclass(frozen=True)
class RuntimeContext:
    runtime_id: str
    environment_id: str
    config_path: Path
    state_dir: Path
    executable: str = "openclaw"
    lifecycle: str = "external"
    service_name: str | None = None
    gateway_port: int | None = None
    home: Path | None = None
    inherit_process_env: bool = True
    extra_env: tuple = ()

    def environment(self):
        result = dict(self.extra_env)
        if self.home:
            result.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"),
                          XDG_DATA_HOME=str(self.home / ".local/share"), XDG_CACHE_HOME=str(self.home / ".cache"))
        return result

    @contextmanager
    def activate(self):
        token = CURRENT_RUNTIME.set(self)
        try:
            yield self
        finally:
            CURRENT_RUNTIME.reset(token)
