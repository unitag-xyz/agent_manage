"""Contract shared by the service and each runtime implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..core.contracts import FUTURE_METHODS, METHODS
from ..core.errors import APIError
from ..core.runtime import RuntimeContext

if TYPE_CHECKING:
    from ..core.service import Workers


@runtime_checkable
class ObservedNativeRuns(Protocol):
    """Optional: recognize stored Runs discovered without a Manager operation."""

    def is_observed_native_run(self, data: dict) -> bool: ...


@dataclass(frozen=True)
class HostIsolation:
    binding_id: str
    local_roots: tuple[Path, ...]
    # Namespaced opaque identities for native resources that tenants may not share.
    exclusive_resources: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class InlineAttachments:
    image_types: tuple[str, ...]
    file_types: tuple[str, ...]
    max_image_bytes: int
    image_limit_message: str


class HostBoundRuntime(Protocol):
    @property
    def host_isolation(self) -> HostIsolation | None: ...
    @property
    def instance_chat(self) -> bool: ...
    def resolve_agent(self, identify: str) -> str: ...
    def resolve_installed_agents(self, targets: dict[str, str]) -> dict[str, str]: ...


class InlineAttachmentRuntime(Protocol):
    inline_attachments: InlineAttachments


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
