"""Chat runtime pools isolated by trusted Host container binding."""

import asyncio
import hashlib
import json
import os
import re
import stat
from contextlib import asynccontextmanager
from pathlib import Path

from .adapters.registry import DEFAULT_ENGINE, create_runtime_adapter, platform_host_factory
from .adapters.binding import read_engine_binding
from .core.errors import APIError
from .core.service import Service
from .core.store import canonical
from .file_io import encode_json, stage_bytes, sync_directory
from .path_io import local_path, read_without_links


def shared_listen_path(value, state, pool):
    path = local_path(value)
    if (path.name != "manager.sock" or len(os.fsencode(path)) > 107
            or any(path.is_relative_to(root) for root in (state, pool.bindings, pool.relays))):
        raise ValueError("Shared Manager socket must be a separate private manager.sock")
    info = path.parent.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("Shared Manager export directory must be service-owned with no peer write access")
    return path


class PlatformRuntimePool:
    def __init__(self, root, audience, profile, *, relay_root=None, binding_guard=None):
        expected = {"instanceIdentify", "bindingId", "mountRoot"}
        if not isinstance(profile, dict) or not expected <= profile.keys():
            raise ValueError("An isolated platform network and mount profile is required")
        self.engine = profile.get("engine", DEFAULT_ENGINE)
        factory = platform_host_factory(self.engine)
        if profile.keys() - expected - {"engine"} - factory.profile_fields:
            raise ValueError("An isolated platform network and mount profile is required")
        self.instance = profile["instanceIdentify"]
        self.generation = profile["bindingId"]
        self.mount = local_path(profile["mountRoot"])
        if (not isinstance(self.instance, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,46}[a-z0-9]", self.instance)
                or not isinstance(self.generation, str) or not re.fullmatch(r"[a-f0-9]{64}", self.generation)
                or self.mount == Path(self.mount.anchor)):
            raise ValueError("Invalid isolated platform profile")
        self.host = factory(profile, self.mount, relay_root)
        self.root = root / "platform-runs"
        self.audience = audience
        self.binding_guard = binding_guard
        self.services = {}
        self.lock = asyncio.Lock()
        self.closed = False
        self.session_lock = asyncio.Lock()
        self.sessions = 0
        self.predecessors = ()

    def admits(self, runtime, generation):
        return runtime == self.instance and generation == self.generation

    def registration(self, grant=None):
        return {**self.host.registration(grant), "engine": self.engine}

    def ledger_directory(self, identity):
        # Adopt the first observed generation in place, never guess which
        # retired tenant database to import. Concurrent adopters must agree.
        stable = hashlib.sha256(canonical([self.instance, *identity[2:]]).encode()).hexdigest()
        anchor = self.root / "ledgers" / (stable + ".json")
        try:
            raw = read_without_links(anchor, 128)
        except FileNotFoundError:
            anchor.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = stage_bytes(anchor, encode_json(self.generation))
            try:
                try:
                    os.link(temporary, anchor)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
            sync_directory(anchor.parent)
            raw = read_without_links(anchor, 128)
        try:
            first = json.loads(raw)
        except (ValueError, UnicodeError):
            first = None
        if not isinstance(first, str) or not re.fullmatch(r"[a-f0-9]{64}", first):
            raise APIError("RECOVERY_REQUIRED", "Invalid private Chat ledger anchor")
        return self.root / hashlib.sha256(canonical([self.instance, first, *identity[2:]]).encode()).hexdigest()

    async def runtime_service(self, grant=None):
        identity = [self.instance, self.generation]
        # Preserve existing OpenClaw receipts; never adopt a different engine's native IDs.
        if self.engine != DEFAULT_ENGINE:
            identity.append({"engine": self.engine})
        identity.extend(["instance-chat"] if grant is None or grant.instance_chat else
                        [grant.installation_id, grant.agent_identify, grant.native_agent_id])
        key = hashlib.sha256(canonical(identity).encode()).hexdigest()
        async with self.lock:
            if self.predecessors:
                await asyncio.gather(*(asyncio.shield(task) for task in self.predecessors))
                self.predecessors = ()
            if self.binding_guard:
                self.binding_guard()
            if self.closed:
                raise APIError("RUNTIME_MISMATCH", "The Host binding is no longer current")
            current = self.services.get(key)
            if current is None:
                if len(self.services) >= 8:
                    raise APIError("BUSY", "Platform pilot capacity reached", retryable=True)
                registration = self.registration(grant)
                current = Service(self.ledger_directory(identity), audience=self.audience, workers=2, binding_guard=self.binding_guard,
                                  session_directory=self.root / key)
                try:
                    adapter = create_runtime_adapter(registration, current.store, current.read_workers, current.publish)
                    self.host.prepare(adapter, idle=self.binding_guard is not None and self.sessions == 0)
                    current.register(adapter, persist_registration=False)
                    await current.start()
                    current.store.bind_ledger(self.instance, self.generation)
                except BaseException:
                    await current.close()
                    raise
                self.services[key] = current
        return current

    async def probe_ready(self):
        await self.host.verify_readiness()
        current = await self.runtime_service()
        await self.host.connect(current.adapter(self.instance))

    async def open(self, grant):
        if not self.admits(grant.runtime_id, grant.binding_id):
            raise APIError("RUNTIME_MISMATCH", "Host offer does not match this Manager sandbox")
        await self.host.verify_grant(grant)
        current = await self.runtime_service(grant)
        adapter = current.adapter(grant.runtime_id)
        agent = await self.host.resolve_grant_agent(adapter, current.read_workers, grant)
        await self.host.connect(adapter)
        return current, current.auth.open_platform_lease(grant, agent), agent

    async def close(self):
        async with self.lock:
            self.closed = True
            services, self.services = tuple(self.services.values()), {}
        await asyncio.gather(*(service.close() for service in services))

    async def acquire(self):
        async with self.session_lock:
            if self.closed:
                raise APIError("RUNTIME_MISMATCH", "The Host binding is no longer current")
            self.sessions += 1
            if self.sessions == 1:
                for service in self.services.values():
                    self.host.resume(service.adapter(self.instance))

    async def release(self):
        async with self.session_lock:
            self.sessions -= 1
            if self.sessions == 0:
                adapters = [service.adapter(self.instance) for service in self.services.values()]
                for adapter in adapters:
                    self.host.pause(adapter)
                await asyncio.gather(*(self.host.disconnect(adapter) for adapter in adapters))


class SharedPlatformRuntimePool:
    """Read one root-owned binding per runtime; no browser supplies paths or endpoints."""

    def __init__(self, root, audience, settings):
        if not isinstance(settings, dict) or set(settings) != {"bindingDirectory", "relayDirectory"}:
            raise ValueError("Expected private binding and relay directories")
        self.bindings = local_path(settings["bindingDirectory"])
        self.relays = local_path(settings["relayDirectory"])
        for directory in (self.bindings, self.relays):
            for parent in (directory, *directory.parents):
                info = parent.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError("Shared Manager directories require trusted root-owned ancestors")
        self.root, self.audience = root, audience
        self.pools = {}
        self.retiring = set()
        self.draining = {}

    def profile(self, runtime):
        if not isinstance(runtime, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,46}[a-z0-9]", runtime):
            return None
        return read_engine_binding(self.bindings / (runtime + ".json"), runtime)

    def admits(self, runtime, generation):
        value = self.profile(runtime)
        if value is None or value["bindingId"] != generation:
            return False
        current = self.pools.get(runtime)
        return current is None or current[0]["bindingId"] != generation or current[0] == value

    def select(self, runtime, generation):
        value = self.profile(runtime)
        if value is None or value["bindingId"] != generation:
            raise APIError("RUNTIME_MISMATCH", "The Host binding is no longer current")
        current = self.pools.get(runtime)
        if current is not None and current[0] == value:
            return current[1]
        if current is not None and current[0]["bindingId"] == generation:
            raise APIError("RUNTIME_MISMATCH", "A Host binding cannot change within a generation")
        if current is None and len(self.pools) >= 512:
            raise APIError("BUSY", "Host runtime capacity reached", retryable=True)
        mount = local_path(value["mountRoot"])
        if any(mount.is_relative_to(private) or private.is_relative_to(mount)
               for private in (self.root, self.bindings, self.relays)):
            raise APIError("RUNTIME_MISMATCH", "Runtime mounts cannot overlap Manager control files")
        for other_runtime, (_, other_pool) in self.pools.items():
            if other_runtime != runtime and (other_pool.generation == generation
                    or mount.is_relative_to(other_pool.mount) or other_pool.mount.is_relative_to(mount)):
                raise APIError("RUNTIME_MISMATCH", "Runtime mounts and relay bindings must be distinct")
        def require_current():
            if self.profile(runtime) != value:
                raise APIError("RUNTIME_MISMATCH", "The Host binding is no longer current")
        factory = platform_host_factory(value.get("engine", DEFAULT_ENGINE))
        pool = PlatformRuntimePool(self.root, self.audience, factory.shared_profile(value, self.relays),
                                   relay_root=self.relays, binding_guard=require_current)
        self.pools[runtime] = (value, pool)
        if current is not None:
            self.retire(current[1])
        pool.predecessors = tuple(self.draining.get(runtime, ()))
        return pool

    def retire(self, pool):
        task = asyncio.create_task(pool.close())
        self.retiring.add(task)
        pending = self.draining.setdefault(pool.instance, set())
        pending.add(task)
        def finished(done):
            self.retiring.discard(done)
            pending.discard(done)
            if not pending:
                self.draining.pop(pool.instance, None)
        task.add_done_callback(finished)

    def reap(self):
        for runtime, (value, pool) in tuple(self.pools.items()):
            current = self.profile(runtime)
            if current is None or current["bindingId"] != value["bindingId"]:
                del self.pools[runtime]
                self.retire(pool)

    async def probe_ready(self, runtime, generation):
        pool = self.select(runtime, generation)
        await pool.host.probe_shared_relay()
        if not self.admits(runtime, generation):
            raise APIError("RUNTIME_MISMATCH", "The Host binding changed during readiness")

    @asynccontextmanager
    async def session(self, grant):
        if not grant.instance_chat:
            raise APIError("FORBIDDEN", "Shared Host Chat requires an instance grant")
        pool = self.select(grant.runtime_id, grant.binding_id)
        await pool.acquire()
        try:
            result = await pool.open(grant)
            try:
                if not self.admits(grant.runtime_id, grant.binding_id):
                    raise APIError("RUNTIME_MISMATCH", "The Host binding changed during authorization")
                yield result
            finally:
                result[0].auth.revoke(result[1].context["leaseId"])
        finally:
            await pool.release()

    async def close(self):
        await asyncio.gather(*(pool.close() for _, pool in self.pools.values()), *self.retiring)
