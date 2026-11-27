"""Contract shared by the service and each runtime implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

from ..core.contracts import FUTURE_METHODS, METHODS
from ..core.errors import APIError
from ..core.runtime import RuntimeContext

if TYPE_CHECKING:
    from ..core.service import Workers
    from ..platform_exchange import PlatformChatGrant


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


@dataclass(frozen=True)
class ResolvedAgentBinding:
    agent_id: str
    installation_id: str


class HostBoundRuntime(Protocol):
    @property
    def host_isolation(self) -> HostIsolation | None: ...
    @property
    def instance_chat(self) -> bool: ...
    def resolve_agent_binding(self, identify: str) -> ResolvedAgentBinding: ...
    def resolve_installed_agents(self, targets: dict[str, str]) -> dict[str, str]: ...


class InlineAttachmentRuntime(Protocol):
    inline_attachments: InlineAttachments


@runtime_checkable
class ThreadWatchRuntime(Protocol):
    def watch_thread(self, thread_id: str) -> Callable[[], None]: ...


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


class PlatformHost(Protocol):
    profile_fields: set[str]
    shared_profile_fields: set[str]

    def __init__(self, profile: dict, mount: Path, relay_root: Path | None): ...
    @staticmethod
    def shared_profile(profile: dict, relay_root: Path) -> dict: ...
    @property
    def has_private_relay(self) -> bool: ...
    def registration(self, grant: PlatformChatGrant | None = None) -> dict: ...
    def prepare(self, adapter: RuntimeAdapter, *, idle: bool) -> None: ...
    async def verify_readiness(self) -> None: ...
    async def probe_shared_relay(self) -> None: ...
    async def verify_grant(self, grant: PlatformChatGrant) -> None: ...
    async def resolve_grant_agent(self, adapter: RuntimeAdapter, workers: Workers, grant: PlatformChatGrant) -> str: ...
    async def connect(self, adapter: RuntimeAdapter) -> None: ...
    def resume(self, adapter: RuntimeAdapter) -> None: ...
    def pause(self, adapter: RuntimeAdapter) -> None: ...
    async def disconnect(self, adapter: RuntimeAdapter) -> None: ...


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
