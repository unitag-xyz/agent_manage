"""Trusted engine routing. CLI-only installations do not need the server extra."""

from dataclasses import dataclass
from typing import Callable

from .execution import LocalExecution, LocalOptions
from .openclaw.factory import create_adapter as create_openclaw_adapter, create_local as open_local
from .openclaw.platform_host import OpenClawPlatformHost


@dataclass(frozen=True)
class EngineDriver:
    open_local: Callable[[LocalOptions], LocalExecution] | None
    create_runtime: Callable | None
    platform_host: type | None = None


DEFAULT_ENGINE = "openclaw"
ENGINES = {DEFAULT_ENGINE: EngineDriver(open_local, create_openclaw_adapter, OpenClawPlatformHost)}


def resolve_engine(engine):
    driver = ENGINES.get(engine) if isinstance(engine, str) else None
    if driver is None:
        raise ValueError("No adapter registered for runtime engine")
    return driver


def platform_host_factory(engine):
    factory = resolve_engine(engine).platform_host
    if factory is None:
        raise ValueError("Runtime engine does not support platform Host binding")
    return factory


def create_runtime_adapter(registration, store, workers, publish):
    factory = resolve_engine(registration.get("engine", DEFAULT_ENGINE)).create_runtime
    if factory is None:
        raise ValueError("Runtime engine does not support the unified service")
    return factory(registration, store, workers, publish)
