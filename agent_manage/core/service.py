"""Transport-independent authorization, admission and durable command dispatch."""
import asyncio
import contextvars
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..adapters.base import ObservedNativeRuns, RuntimeAdapter
from .auth import LeaseAuthority
from .contracts import METHODS, schema
from .errors import APIError
from .store import Store, canonical
from .runtime import CURRENT_WRITE_GUARD

LOG = logging.getLogger("agent_manage.api")
MAX_ATTACHMENT_REQUEST = 24 * 1024 * 1024
MAX_INLINE_ATTACHMENT_BASE64 = 23 * 1024 * 1024

MAX_INLINE_ATTACHMENT_COUNT = 5
LARGE_ATTACHMENT_OPERATIONS = threading.BoundedSemaphore(2)


class Workers:
    def __init__(self, count, name, queue_limit=0):
        self.pool = ThreadPoolExecutor(max_workers=count, thread_name_prefix=name)
        self.capacity, self.active = count, 0
        self.waiting, self.queue_limit = 0, queue_limit
        self.slots = asyncio.Semaphore(count)

    async def __call__(self, function, *args):
        if self.active + self.waiting >= self.capacity + self.queue_limit:
            raise APIError("BUSY", "Worker capacity reached; retry later", retryable=True)
        self.waiting += 1
        try:
            await self.slots.acquire()
        finally:
            self.waiting -= 1
        self.active += 1
        def execute():
            guard = CURRENT_WRITE_GUARD.get()
            if guard:
                guard()
            return function(*args)
        try:
            future = asyncio.get_running_loop().run_in_executor(self.pool, contextvars.copy_context().run, execute)
        except Exception:
            self.active -= 1
            self.slots.release()
            raise
        def finished(_):
            self.active -= 1
            self.slots.release()
        future.add_done_callback(finished)
        return await asyncio.shield(future)

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


def safe_result(value):
    """Management metadata is public; native logs and secret-shaped fields are not."""
    if isinstance(value, dict):
        return {key: safe_result(child) for key, child in value.items()
                if not re.search(r"token|secret|password|api.?key|credential|authorization", key, re.I)
                and key not in ("stdout", "stderr", "command", "command_text", "argv", "raw")}
    if isinstance(value, list):
        return [safe_result(child) for child in value]
    return value


class Service:
    def __init__(self, directory, *, audience="agent-manager", workers=4, binding_guard=None, session_directory=None):
        self.binding_guard = binding_guard
        self.store = Store(directory)
        self.auth = LeaseAuthority(self.store if session_directory is None or session_directory == directory
                                   else Store(session_directory), audience)
        self.read_workers = Workers(workers, "manager-read")
        self.write_workers = Workers(2, "manager-control", queue_limit=16)
        self.media_workers = Workers(2, "manager-media")
        self.adapters: dict[str, RuntimeAdapter] = {}
        self.tasks = set()
        self.listeners = set()
        self.sequence = 0
        self.epoch = __import__("uuid").uuid4().hex
        self.closed = False
        self._owner = None

    async def start(self):
        from .transactions import runtime_write_lock
        owner = runtime_write_lock(self.store.path.parent, timeout=0)
        owner.__enter__()
        self._owner = owner
        self.store.recover_operations()
        if self.auth.store is not self.store:
            self.auth.store.prune_credentials()
        for identifier in self.adapters:
            self.store.lose_run_observation(identifier)

    def register(self, adapter: RuntimeAdapter, *, persist_registration=True):
        if adapter.runtime_id in self.adapters:
            raise ValueError("Duplicate runtime registration")
        context = adapter.context
        binding = getattr(adapter, "host_isolation", None)
        if binding:
            private = self.store.path.parent
            if any(local.is_relative_to(private) or private.is_relative_to(local) for local in binding.local_roots):
                raise ValueError("Manager private state must not overlap tenant mounts")
        for existing in self.adapters.values():
            other = getattr(existing, "host_isolation", None)
            local_roots = binding.local_roots if binding else (context.state_dir,)
            previous_roots = other.local_roots if other else (existing.context.state_dir,)
            if binding or other:
                if any(left.is_relative_to(right) or right.is_relative_to(left)
                       for left in local_roots for right in previous_roots):
                    raise ValueError("Host-bound runtimes must not overlap local mounts")
            if binding and other and not binding.exclusive_resources.isdisjoint(other.exclusive_resources):
                raise ValueError("Host-bound runtimes require distinct native resource identities")
            left, right = existing.context.state_dir, context.state_dir
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise ValueError("Runtimes must not overlap state directories")
            if existing.context.environment_id != context.environment_id and existing.context.home == context.home:
                raise ValueError("Different environments need explicit distinct home directories")
        self.adapters[adapter.runtime_id] = adapter
        adapter.control_workers = self.write_workers
        adapter.media_workers = self.media_workers
        if persist_registration:
            self.store.object("runtime", adapter.runtime_id, adapter.runtime_id,
                {"engine": adapter.engine, "environmentId": context.environment_id, "configPath": str(context.config_path)})

    def adapter(self, runtime) -> RuntimeAdapter:
        if runtime not in self.adapters:
            raise APIError("NOT_FOUND", "Runtime is not registered")
        return self.adapters[runtime]

    def authorize(self, lease, method, params):
        if self.binding_guard is not None:
            self.binding_guard()
        spec = METHODS[method]
        runtime = params.get("runtimeId")
        lease = self.auth.authorize(lease, method, runtime)
        if spec.scope == "operation":
            operation = self.store.operation(params["operationId"], lease.context, runtime)
            return self.auth.authorize(lease, method, runtime, operation["agentId"], runtime_wide=operation["agentId"] is None)
        adapter = self.adapter(runtime) if runtime else None
        agent = adapter.resolve_scope(method, params) if adapter else None
        lease = self.auth.authorize(lease, method, runtime, agent, runtime_wide=spec.scope == "runtime"
                                    or method == "runs.create" and bool(params.get("attachments")))
        if method == "runs.cancel" and getattr(adapter, "instance_chat", False):
            run = self.store.get(params["runId"], kind="run", runtime=runtime)
            operation_id = run["data"].get("operationId")
            if not operation_id:
                if isinstance(adapter, ObservedNativeRuns) and adapter.is_observed_native_run(run["data"]) is True:
                    return lease
                raise APIError("FORBIDDEN", "The Run has no verifiable Chat operation owner")
            try:
                operation = self.store.operation(operation_id, lease.context, runtime)
            except APIError as exc:
                raise APIError("FORBIDDEN", "The Run belongs to a different Chat operator") from exc
            if operation["method"] != "runs.create" or operation["threadId"] != run["data"].get("threadId"):
                raise APIError("FORBIDDEN", "The Run has no matching Chat operation")
        return lease

    def validate(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("requestId"), str) or not 1 <= len(request["requestId"]) <= 128:
            raise APIError("INVALID_ARGUMENT", "A requestId of 1-128 characters is required")
        if set(request) - {"requestId", "method", "params", "type"}:
            raise APIError("INVALID_ARGUMENT", "Unknown request field")
        method, params = request.get("method"), request.get("params", {})
        if not isinstance(method, str) or method not in METHODS:
            raise APIError("UNSUPPORTED", "Unknown or unsupported method")
        if not isinstance(params, dict):
            raise APIError("INVALID_ARGUMENT", "params must be an object")
        spec = METHODS[method]
        allowed = {"runtimeId", *spec.required, *spec.optional}
        if spec.write:
            allowed.add("clientRequestId")
        if set(params) - allowed or any(field not in params for field in spec.required):
            raise APIError("INVALID_ARGUMENT", "Parameters do not match the method contract", details={"required": list(spec.required)})
        if spec.scope != "system" and not isinstance(params.get("runtimeId"), str):
            raise APIError("INVALID_ARGUMENT", "runtimeId is required")
        if spec.write and (not isinstance(params.get("clientRequestId"), str) or not 1 <= len(params["clientRequestId"]) <= 128):
            raise APIError("INVALID_ARGUMENT", "Writes require clientRequestId of 1-128 characters")
        if method == "operations.lookup" and (not isinstance(params["clientRequestId"], str)
                or not 1 <= len(params["clientRequestId"]) <= 128):
            raise APIError("INVALID_ARGUMENT", "Operation lookup requires clientRequestId of 1-128 characters")
        if "operationMethod" in params and (not isinstance(params["operationMethod"], str)
                or params["operationMethod"] not in METHODS or not METHODS[params["operationMethod"]].write):
            raise APIError("INVALID_ARGUMENT", "Expected a registered write method")
        for key in ("agentId", "threadId", "segmentId", "messageId", "runId", "resourceId", "operationId", "modelId", "title", "message"):
            if key == "modelId" and method == "threads.models.update" and params["modelId"] is None:
                continue
            if key in params and (not isinstance(params[key], str) or not params[key] or len(params[key]) > (256000 if key == "message" else 8192)):
                raise APIError("INVALID_ARGUMENT", f"Invalid {key}")
        if method == "threads.models.update" and isinstance(params["modelId"], str) and len(params["modelId"]) > 256:
            raise APIError("INVALID_ARGUMENT", "Invalid modelId")
        if method == "threads.thinking.update" and params["thinkingLevel"] is not None:
            level = params["thinkingLevel"]
            if not isinstance(level, str) or not 1 <= len(level) <= 32 or not level.isascii() or not level.isalnum():
                raise APIError("INVALID_ARGUMENT", "Invalid thinkingLevel")
        if "expectedCurrentSegmentId" in params and params["expectedCurrentSegmentId"] is not None and (not isinstance(params["expectedCurrentSegmentId"], str) or not 1 <= len(params["expectedCurrentSegmentId"]) <= 128):
            raise APIError("INVALID_ARGUMENT", "expectedCurrentSegmentId must be a segment ID or null for a new empty thread")
        if "limit" in params and (type(params["limit"]) is not int or not 1 <= params["limit"] <= 200):
            raise APIError("INVALID_ARGUMENT", "limit must be 1-200")
        if "taskId" in params and (not isinstance(params["taskId"], str) or not 1 <= len(params["taskId"]) <= 128):
            raise APIError("INVALID_ARGUMENT", "Invalid taskId")
        if "range" in params and params["range"] not in ("7d", "30d", "90d", "1y"):
            raise APIError("INVALID_ARGUMENT", "Invalid usage range")
        if method == "tasks.list" and "cursor" in params and (not isinstance(params["cursor"], str)
                or not 1 <= len(params["cursor"]) <= 12 or not params["cursor"].isascii() or not params["cursor"].isdigit()):
            raise APIError("INVALID_ARGUMENT", "Invalid task cursor")
        if method == "tasks.list" and "status" in params:
            value = params["status"]
            statuses = value if isinstance(value, list) else [value]
            allowed_statuses = ("queued", "running", "completed", "failed", "cancelled", "timed_out")
            if not 1 <= len(statuses) <= len(allowed_statuses) or any(status not in allowed_statuses for status in statuses):
                raise APIError("INVALID_ARGUMENT", "Invalid task status filter")
        if method == "resources.get" and ("offset" in params or "length" in params):
            if (type(params.get("offset")) is not int or not 0 <= params["offset"] <= (1 << 53) - 1
                    or type(params.get("length")) is not int or not 1 <= params["length"] <= 128 * 1024):
                raise APIError("INVALID_ARGUMENT", "Expected offset and length up to 128 KiB")
        if method == "runs.create" and "attachments" in params:
            import base64
            attachments = params["attachments"]
            if not isinstance(attachments, list) or len(attachments) > MAX_INLINE_ATTACHMENT_COUNT:
                raise APIError("INVALID_ARGUMENT", "At most five inline attachments are supported")
            total = 0
            adapter = self.adapters.get(params["runtimeId"])
            policy = getattr(adapter, "inline_attachments", None)
            if attachments and adapter is not None and adapter.supported and policy is None:
                raise APIError("UNSUPPORTED", "Runtime does not support inline attachments")
            for item in attachments:
                if not isinstance(item, dict) or item.keys() != {"type", "mimeType", "fileName", "content"}:
                    raise APIError("INVALID_ARGUMENT", "Invalid attachment fields")
                kind, mime, name, content = (item[key] for key in ("type", "mimeType", "fileName", "content"))
                if (not isinstance(name, str) or not 1 <= len(name) <= 128 or name in (".", "..")
                        or any(char in name for char in "/\\:") or not name.isprintable()
                        or not isinstance(content, str) or not content
                        or not isinstance(mime, str)
                        or not (kind == "image" and (policy is None or mime in policy.image_types)
                                or kind == "file" and (policy is None or mime in policy.file_types))):
                    raise APIError("INVALID_ARGUMENT", "Invalid inline attachment")
                total += len(content)
                if total > MAX_INLINE_ATTACHMENT_BASE64:
                    raise APIError("REQUEST_TOO_LARGE", "Inline attachments exceed the Chat budget")
                try:
                    decoded_bytes = len(base64.b64decode(content, validate=True))
                    if not decoded_bytes:
                        raise ValueError("empty attachment")
                except ValueError as exc:
                    raise APIError("INVALID_ARGUMENT", "Attachment content must be base64") from exc
                if kind == "image" and policy is not None and decoded_bytes > policy.max_image_bytes:
                    raise APIError("REQUEST_TOO_LARGE", policy.image_limit_message)
        if "omitThinking" in params and type(params["omitThinking"]) is not bool:
            raise APIError("INVALID_ARGUMENT", "omitThinking must be a boolean")
        if "options" in params and not isinstance(params["options"], dict):
            raise APIError("INVALID_ARGUMENT", "options must be an object")
        if "edge" in params and (params["edge"] not in ("start", "end") or "cursor" in params):
            raise APIError("INVALID_ARGUMENT", "edge must be start/end and cannot accompany cursor")
        if "view" in params and params["view"] not in ("full", "compact"):
            raise APIError("INVALID_ARGUMENT", "Unknown message projection")
        return method, params

    async def dispatch(self, lease, request):
        started = time.monotonic()
        request_id = request.get("requestId") if isinstance(request, dict) else None
        method = None
        release_attachment_slot = False
        try:
            method, params = self.validate(request)
            lease = self.authorize(lease, method, params)
            if self.closed:
                raise APIError("NOT_READY", "Manager is shutting down")
            if METHODS[method].write:
                if len(self.tasks) >= 16:
                    raise APIError("BUSY", "Command capacity reached", retryable=True)
                if method == "runs.create" and sum(len(item["content"]) for item in params.get("attachments", ())) > 1024 * 1024:
                    release_attachment_slot = LARGE_ATTACHMENT_OPERATIONS.acquire(blocking=False)
                    if not release_attachment_slot:
                        raise APIError("BUSY", "Large attachment capacity reached", retryable=True)
                adapter = self.adapter(params["runtimeId"])
                agent = adapter.resolve_scope(method, params) if METHODS[method].scope != "runtime" else None
                thread = params.get("threadId")
                if method == "runs.cancel":
                    thread = self.store.get(params["runId"], kind="run", runtime=params["runtimeId"])["data"]["threadId"]
                operation, created = self.store.claim(lease.context, params["runtimeId"], method, params["clientRequestId"], params,
                                                     agent_id=agent, thread_id=thread)
                if created:
                    task = asyncio.create_task(self.execute(operation["operationId"], lease, method, params))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                    if release_attachment_slot:
                        task.add_done_callback(lambda _: LARGE_ATTACHMENT_OPERATIONS.release())
                        release_attachment_slot = False
                result = operation
            else:
                result = await self.read(lease, method, params)
            # Recheck even if expiry/revocation happened while native I/O was in flight.
            lease = self.authorize(lease, method, params)
            return {"requestId": request_id, "ok": True, "result": result, "authContext": lease.public(),
                    "timings": {"managerElapsedMs": round((time.monotonic() - started) * 1000, 3)}}
        except APIError as exc:
            return self.error_response(lease, request_id, exc)
        except Exception as exc:
            return self.error_response(lease, request_id, self.translate_error(exc))
        finally:
            if release_attachment_slot:
                LARGE_ATTACHMENT_OPERATIONS.release()
            LOG.info("rpc method=%s duration_ms=%.2f", method, (time.monotonic() - started) * 1000)

    def error_response(self, lease, identifier, error):
        context = {}
        try:
            context = {"authContext": self.auth.authenticate(lease.token).public()}
        except APIError:
            pass
        return {"requestId": identifier, "ok": False, "error": error.payload(), **context}

    @staticmethod
    def translate_error(exc):
        if isinstance(exc, APIError):
            return exc
        from ..response import ProtocolError
        if isinstance(exc, ProtocolError):
            return APIError(exc.code, str(exc))
        if isinstance(exc, (ValueError, TypeError)):
            return APIError("INVALID_ARGUMENT", "Native command rejected the supplied arguments")
        if isinstance(exc, FileNotFoundError):
            return APIError("NOT_FOUND", "A required local file or executable was not found")
        LOG.error("native operation failed type=%s", type(exc).__name__)
        return APIError("OPERATION_FAILED", "Native operation failed; inspect protected host logs and backups")

    async def execute(self, operation_id, lease, method, params):
        guard_token = CURRENT_WRITE_GUARD.set(lambda: self.authorize(lease, method, params))
        try:
            self.authorize(lease, method, params)
            self.store.finish(operation_id, "running")
            adapter = self.adapter(params["runtimeId"])
            result = await adapter.invoke(method, {**params, "_operationId": operation_id})
            result = safe_result(result)
            if len(canonical(result).encode()) > 1024 * 1024:
                raise APIError("RESULT_TOO_LARGE", "Command completed but result exceeds the metadata budget")
            self.store.finish(operation_id, "completed", result=result)
        except Exception as exc:
            error = self.translate_error(exc)
            # A timeout or a generic native failure can have partial external effects.
            status = "unknown" if error.code in ("DISPATCH_UNKNOWN", "OPERATION_FAILED", "RESULT_TOO_LARGE") or (error.details or {}).get("partialEffects") else "failed"
            self.store.finish(operation_id, status, error=error.payload())
        finally:
            CURRENT_WRITE_GUARD.reset(guard_token)

    async def read(self, lease, method, params):
        if method == "system.info":
            from .. import __version__
            return {"apiVersion": "1.0", "implementationVersion": __version__, "epoch": self.epoch,
                    "transports": ["http", "websocket"], "methods": schema(), "eventReplay": False}
        if method in ("runtimes.list", "targets.list", "environments.list"):
            allowed = [adapter for identifier, adapter in self.adapters.items()
                       if identifier in lease.context["runtimeIds"] or "*" in lease.context["runtimeIds"]]
            if method == "runtimes.list":
                return {"items": [self.runtime_info(adapter) for adapter in allowed]}
            if method == "environments.list":
                return {"items": list({adapter.context.environment_id: {"environmentId": adapter.context.environment_id,
                    "targetId": adapter.registration.get("targetId", "local"), "kind": adapter.registration.get("environmentKind", "local"),
                    "lifecycleOwner": "external"} for adapter in allowed}.values())}
            return {"items": [{"targetId": identifier, "kind": "local"} for identifier in sorted({adapter.registration.get("targetId", "local") for adapter in allowed})]}
        adapter = self.adapter(params["runtimeId"])
        if method == "capabilities.get":
            result = adapter.capabilities()
            for action, value in result["methods"].items():
                value["authorized"] = "*" in lease.context["actions"] or action in lease.context["actions"]
                if action in METHODS and METHODS[action].scope == "runtime" and "*" not in lease.context["agentIds"]:
                    value["authorized"] = False
            policy = getattr(adapter, "inline_attachments", None)
            if (policy is not None and getattr(adapter, "instance_chat", False) and "*" in lease.context["agentIds"]
                    and result["methods"]["runs.create"]["authorized"]):
                result["attachments"] = {"mode": "inline", "maxCount": MAX_INLINE_ATTACHMENT_COUNT,
                    "maxBase64Bytes": MAX_INLINE_ATTACHMENT_BASE64, "maxImageBytes": policy.max_image_bytes,
                    "maxRequestBytes": MAX_ATTACHMENT_REQUEST,
                    "mimeTypes": [*policy.image_types, *policy.file_types]}
            return result
        if method == "runtimes.get":
            return self.runtime_info(adapter)
        if method == "operations.get":
            return self.store.operation(params["operationId"], lease.context, params["runtimeId"])
        if method in ("operations.list", "operations.lookup"):
            return {"items": self.store.operations(lease.context, params["runtimeId"], params.get("clientRequestId"),
                agent_ids=lease.context["agentIds"], thread_id=params.get("threadId"), method=params.get("operationMethod"))}
        if method == "runs.lookup":
            operations = self.store.operations(lease.context, params["runtimeId"], params["clientRequestId"],
                agent_ids=lease.context["agentIds"], thread_id=params["threadId"], method="runs.create")
            result = await adapter.invoke(method, params)
            permitted = {op["operationId"] for op in operations}
            result["items"] = [run for run in result["items"] if run.get("operationId") in permitted]
            return {**result, "operations": operations, "acceptance": "unknown" if any(op["status"] == "unknown" for op in operations)
                    else "accepted" if operations else "not_accepted"}
        if method.startswith("events.") or method in ("sessions.subscribe", "sessions.unsubscribe"):
            raise APIError("TRANSPORT_REQUIRED", "Subscriptions require WebSocket transport")
        result = await adapter.invoke(method, params)
        if method == "agents.list" and "*" not in lease.context["agentIds"]:
            result["items"] = [item for item in result["items"] if item["agentId"] in lease.context["agentIds"]]
        return result

    @staticmethod
    def runtime_info(adapter):
        return {"runtimeId": adapter.runtime_id, "environmentId": adapter.context.environment_id,
                "engine": adapter.engine, "engineVersion": adapter.version,
                "lifecycleOwner": adapter.context.lifecycle, "supported": adapter.supported}

    async def publish(self, runtime, thread, payload):
        self.sequence += 1
        event = {"runtimeId": runtime, "threadId": thread, "epoch": self.epoch,
                 "sequence": self.sequence, **payload}
        for listener in tuple(self.listeners):
            listener(event)

    async def close(self):
        self.closed = True
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for adapter in self.adapters.values():
            await adapter.close()
        for pool in (self.read_workers, self.write_workers, self.media_workers):
            pool.close()
        if self._owner:
            self._owner.__exit__(None, None, None)
            self._owner = None
