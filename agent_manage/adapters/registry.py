"""Select an engine factory for each registered runtime."""

from .base import RuntimeAdapter
from .openclaw.factory import create_adapter as create_openclaw_adapter

FACTORIES = {"openclaw": create_openclaw_adapter}


def create_runtime_adapter(registration, store, workers, publish) -> RuntimeAdapter:
    engine = registration.get("engine", "openclaw")
    factory = FACTORIES.get(engine) if isinstance(engine, str) else None
    if factory is None:
        raise ValueError(f"No adapter registered for runtime engine {engine!r}")
    return factory(registration, store, workers, publish)
