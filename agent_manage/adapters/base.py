"""Contract shared by the service and each runtime implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..core.contracts import FUTURE_METHODS, METHODS
from ..core.errors import APIError
from ..core.runtime import RuntimeContext

if TYPE_CHECKING:
    from ..core.service import Workers


class RuntimeAdapter(Protocol):
    runtime_id: str
    engine: str
    version: str
    supported: bool
    context: RuntimeContext
    registration: dict
    control_workers: Workers
    media_workers: Workers

    def capabilities(self) -> dict: ...
    def resolve_scope(self, method: str, params: dict) -> str | None: ...
    async def invoke(self, method: str, params: dict) -> dict: ...
    async def resource_chunk(
        self, identifier: str, cursor: str | None = None, *,
        offset: int | None = None, length: int | None = None,
    ) -> dict: ...
    async def close(self) -> None: ...


class UnsupportedRuntimeAdapter(RuntimeAdapter):
    """Expose a detected but unmatched runtime without accessing its native data."""

    supported = False

    def __init__(self, registration: dict, context: RuntimeContext, *, engine: str, version: str):
        self.registration = registration
        self.context = context
        self.runtime_id = context.runtime_id
        self.engine = engine
        self.version = version

    def capabilities(self) -> dict:
        return {
            "runtimeId": self.runtime_id,
            "engine": self.engine,
            "engineVersion": self.version,
            "adapter": None,
            "adapterVersion": None,
            "methods": {method: {"support": "unsupported", "readiness": "unavailable"}
                        for method in (*METHODS, *FUTURE_METHODS)},
            "testedVersions": [],
            "validation": "unverified",
            "eventReplay": False,
            "historyWatermark": False,
            "lifecycleOwner": self.context.lifecycle,
        }

    def _unsupported(self) -> APIError:
        return APIError("UNSUPPORTED_VERSION", f"No adapter matches {self.engine} version {self.version}")

    def resolve_scope(self, method: str, params: dict) -> str | None:
        if any(key in params for key in ("agentId", "threadId", "runId", "resourceId")):
            raise self._unsupported()
        return None

    async def invoke(self, method: str, params: dict) -> dict:
        raise self._unsupported()

    async def resource_chunk(self, identifier, cursor=None, *, offset=None, length=None) -> dict:
        raise self._unsupported()

    async def close(self) -> None:
        pass
