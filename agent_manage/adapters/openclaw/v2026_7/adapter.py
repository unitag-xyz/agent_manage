"""OpenClaw 2026.7.x adapter. Management delegates to existing execution services."""
import asyncio
import base64
import hashlib
import json
import re
import uuid
from collections import OrderedDict
from pathlib import Path

from .... import __version__, models
from ....core.contracts import METHODS, FUTURE_METHODS
from ....core.errors import APIError
from ....core.store import canonical, now_ms
from ....local import LocalRunner
from ....orchestrator import InstanceManagerV2
from ....segment_reader import SegmentReader, read_segment, safe_file
from ....segment_timing import measured_read
from ...base import RuntimeAdapter
from .gateway import Gateway
from .projection import message_dto

NATIVE = {"threads.create": "sessions.create", "threads.reset": "sessions.reset",
          "threads.update": "sessions.patch", "threads.delete": "sessions.delete",
          "runs.create": "chat.send", "runs.cancel": "chat.abort", "events.subscribe": "chat.send"}


class OpenClawAdapter(RuntimeAdapter):
    engine = "openclaw"
    supported = True

    def __init__(self, registration, store, workers, publish, *, context, version):
        self.registration, self.store, self.workers, self.publish = registration, store, workers, publish
        self.context, self.version = context, version
        self.runtime_id = context.runtime_id
        config_path = context.config_path
        self.template_root = registration.get("templateRoot") or str(config_path.parent / "templates")
        self.activity_revisions = OrderedDict()
        self.chat_streams = OrderedDict()
        self.native_writes = asyncio.Lock()
        self.gateway = Gateway(registration["gatewayUrl"], config_path,
            store.path.parent / "devices" / (self.runtime_id + ".key"), self.native_event,
            origin=registration.get("gatewayOrigin"), expected_version=self.version) if registration.get("gatewayUrl") else None

    def manager(self):
        manager = InstanceManagerV2(LocalRunner(self.context.executable), template_root=self.template_root,
                                    config_path=str(self.context.config_path))
        manager.runtime_context = self.context
        if "environmentKind" in self.registration:
            manager.container_runtime = self.registration["environmentKind"] == "container"
        self.redact(manager, self.config())
        return manager

    @staticmethod
    def redact(manager, value):
        if isinstance(value, dict):
            for key, child in value.items():
                if re.search(r"key|token|secret|password", key, re.I) and isinstance(child, str):
                    manager.runner.add_redaction_value(child)
                else:
                    OpenClawAdapter.redact(manager, child)
        elif isinstance(value, list):
            for child in value:
                OpenClawAdapter.redact(manager, child)

    def config(self):
        config = json.loads(self.context.config_path.read_text())
        agents = config.get("agents", {})
        paths = [agents.get("defaults", {}).get("workspace"), config.get("session", {}).get("store")]
        for agent in agents.get("list", []):
            paths.extend([agent.get("workspace"), agent.get("agentDir")])
        if any(isinstance(path, str) and path and not Path(path).is_absolute() for path in paths):
            raise APIError("NOT_READY", "Unified local runtimes require absolute workspace, agentDir and session-store paths")
        return config

    def capabilities(self):
        native_methods = (self.gateway.hello or {}).get("features", {}).get("methods") if self.gateway else None
        methods = {}
        for method in [*METHODS, *FUTURE_METHODS]:
            support = "adapted" if method in METHODS else "unsupported"
            readiness = "ready" if support == "adapted" else "unavailable"
            if method in NATIVE:
                readiness = "not_connected" if self.gateway else "not_configured"
                if native_methods is not None:
                    readiness = "ready"
                    if NATIVE[method] not in native_methods:
                        support, readiness = "unsupported", "unavailable"
            methods[method] = {"support": support, "readiness": readiness}
        return {"runtimeId": self.runtime_id, "engine": "openclaw", "engineVersion": self.version,
                "adapter": "openclaw-july", "adapterVersion": __version__, "methods": methods,
                "testedVersions": ["2026.7.1-2"], "validation": "native-tested" if self.version == "2026.7.1-2" else "unverified",
                "eventReplay": False, "historyWatermark": False, "nativeProtocol": 4,
                "segmentPrecondition": "manager_serialized", "nativeSegmentCAS": False,
                "maxHistoryLimit": 200, "historyByteBudget": 1024 * 1024,
                "lifecycleOwner": self.context.lifecycle}

    def agents(self):
        config = self.config()
        entries = config.get("agents", {}).get("list", [])
        if not entries:
            entries = [{"id": "main"}]
        result = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            identifier = self.store.object("agent", self.runtime_id, entry["id"], {"name": entry.get("name", entry["id"])})
            result.append({"agentId": identifier, "runtimeId": self.runtime_id, "name": entry.get("name", entry["id"]),
                           "extensions": {"openclaw": {"agentId": entry["id"]}},
                           "isDefault": bool(entry.get("default")) or len(entries) == 1})
        return result

    def index(self, agent_id):
        agent = self.store.get(agent_id, kind="agent", runtime=self.runtime_id)
        # Verify the agent still belongs to this runtime, not just an old registry row.
        if agent_id not in {item["agentId"] for item in self.agents()}:
            raise APIError("NOT_FOUND", "Agent is no longer configured")
        root = self.context.state_dir
        configured = self.config().get("session", {}).get("store")
        path = Path(configured.replace("{agentId}", agent["native"].lower())).expanduser() if configured else root / "agents" / agent["native"].lower() / "sessions/sessions.json"
        try:
            path = safe_file(path, [path.parent])
            with path.open() as stream:
                data = json.load(stream)
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise APIError("NATIVE_DATA_ERROR", "Invalid native session index")
        return agent, data

    def threads(self, agent_id):
        agent, index = self.index(agent_id)
        result = []
        for key, entry in index.items():
            if not key.startswith("agent:" + agent["native"] + ":") or not isinstance(entry, dict):
                continue
            identifier = self.store.object("thread", self.runtime_id, key, {"agentId": agent_id})
            result.append({"threadId": identifier, "agentId": agent_id, "title": entry.get("label") or entry.get("displayName"),
                           "updatedAt": entry.get("updatedAt"), "currentSegmentId": self.segment_id(identifier, entry.get("sessionId"))})
        return result

    def thread(self, identifier):
        row = self.store.get(identifier, kind="thread", runtime=self.runtime_id)
        _, index = self.index(row["data"]["agentId"])
        entry = index.get(row["native"])
        if not isinstance(entry, dict):
            raise APIError("NOT_FOUND", "Thread no longer exists")
        return row, entry

    def segment_id(self, thread_id, native):
        if not native:
            return None
        return self.store.object("segment", self.runtime_id, canonical([thread_id, native]),
                                 {"threadId": thread_id, "sessionId": native})

    def resolve_scope(self, method, params):
        if "agentId" in params:
            self.store.get(params["agentId"], kind="agent", runtime=self.runtime_id)
            return params["agentId"]
        if "threadId" in params:
            return self.store.get(params["threadId"], kind="thread", runtime=self.runtime_id)["data"]["agentId"]
        if "runId" in params or "resourceId" in params:
            kind = "run" if "runId" in params else "resource"
            obj = self.store.get(params[kind + "Id"], kind=kind, runtime=self.runtime_id)
            return self.resolve_scope(method, {"threadId": obj["data"]["threadId"]})
        return None

    @measured_read
    def history(self, params, action="history"):
        thread, _ = self.thread(params["threadId"])
        segment = self.store.get(params["segmentId"], kind="segment", runtime=self.runtime_id)
        if segment["data"]["threadId"] != params["threadId"]:
            raise APIError("FORBIDDEN", "Segment belongs to another thread")
        payload = {"root": str(self.context.state_dir), "config": self.config(), "sessionKey": thread["native"],
                   "sessionId": segment["data"]["sessionId"], "action": action, "includeSnapshot": True,
                   "maxTranscriptBytes": 256 * 1024 * 1024, "maxRecordBytes": 16 * 1024 * 1024, "maxRecords": 20000}
        payload.update({key: params[key] for key in ("cursor", "ref", "messageId", "edge", "limit", "view", "omitThinking", "mediaOffset", "mediaLength") if key in params})
        payload.setdefault("edge", "end")
        payload.setdefault("limit", 50)
        if "cursor" not in payload and "ref" not in payload:
            payload.setdefault("view", "compact")
        with self.context.activate():
            result = read_segment(payload, LocalRunner()._command_env())
        return result

    def project(self, message, params, projection):
        def register(reference):
            data = {"threadId": params["threadId"], "segmentId": params["segmentId"], "reference": reference,
                    "role": reference.get("purpose", "unknown")}
            identity = [params["threadId"], params["segmentId"], message["id"], reference.get("mediaId") or reference, data["role"]]
            return self.store.object("resource", self.runtime_id, canonical(identity), data)
        dto = message_dto(message, thread_id=params["threadId"], segment_id=params["segmentId"], projection=projection, register_resource=register)
        if message.get("fullMessageRef"):
            dto["fullMessageRef"] = {"method": "messages.get", "params": {"runtimeId": self.runtime_id,
                "threadId": params["threadId"], "segmentId": params["segmentId"], "messageId": message["id"],
                "ref": message["fullMessageRef"]["params"]["ref"]}}
        return dto

    async def invoke(self, method, params):
        if method in NATIVE:
            return await self.native_call(method, params)
        if method == "agents.list":
            return {"items": await self.workers(self.agents)}
        if method == "threads.list":
            return {"items": await self.workers(self.threads, params["agentId"])}
        if method in ("threads.get", "segments.list"):
            thread, entry = await self.workers(self.thread, params["threadId"])
            if method == "threads.get":
                return {"threadId": thread["id"], "agentId": thread["data"]["agentId"], "title": entry.get("label") or entry.get("displayName"),
                        "updatedAt": entry.get("updatedAt"), "currentSegmentId": self.segment_id(thread["id"], entry.get("sessionId"))}
            current = entry.get("sessionId")
            ids = list(dict.fromkeys([*entry.get("usageFamilySessionIds", []), current]))
            ids = [value for value in ids if value and value != current] + ([current] if current else [])
            return {"threadId": thread["id"], "items": [{"segmentId": self.segment_id(thread["id"], value), "isCurrent": value == current} for value in ids]}
        if method == "messages.list":
            result = await self.workers(self.history, params)
            projection = {"view": result["view"], "omitThinking": result["omitThinking"]}
            projection["projectionId"] = hashlib.sha256(canonical(projection).encode()).hexdigest()[:16]
            items = [self.project(message, params, projection) for message in result["messages"]]
            return {"threadId": params["threadId"], "segmentId": params["segmentId"], "items": items,
                "segment": result["segment"], "olderCursor": result["olderCursor"], "newerCursor": result["newerCursor"],
                "snapshotId": result.get("snapshotId"), "projectionId": projection["projectionId"],
                "boundary": {"reachedUser": result["reachedUser"], "turnComplete": None},
                "bytesReturned": len(canonical(items).encode()), "limitReachedReason": "boundary_or_budget",
                "timings": result.get("timings")}
        if method == "messages.get":
            return await self.full_message(params)
        if method == "resources.get":
            row = self.store.get(params["resourceId"], kind="resource", runtime=self.runtime_id)
            await self.workers(self.thread, row["data"]["threadId"])
            return {"resourceId": row["id"], "threadId": row["data"]["threadId"], "role": row["data"]["role"],
                "availability": "unknown", "bytes": None, "url": f"/v1/resources/{row['id']}?runtimeId={self.runtime_id}"}
        if method in ("runs.get", "runs.lookup"):
            if method == "runs.get":
                return self.store.get(params["runId"], kind="run", runtime=self.runtime_id)["data"]
            return {"items": [row["data"] for row in self.store.objects("run", self.runtime_id)
                    if row["data"].get("clientRequestId") == params["clientRequestId"] and row["data"]["threadId"] == params["threadId"]]}
        if method == "runtimes.recover":
            from ....core.recovery import recover_refreshes
            from ....core.transactions import runtime_write_lock
            def recover():
                with runtime_write_lock(self.context.state_dir):
                    return {"recovered": recover_refreshes(self.context.config_path)}
            return await self.workers(recover)
        pool = getattr(self, "control_workers", self.workers) if METHODS[method].write else self.workers
        return await pool(self.manage, method, params)

    async def full_message(self, params):
        result = await self.workers(self.history, params, "message")
        projection = {"view": result.get("view", "compact"), "omitThinking": result.get("omitThinking", False)}
        projection["projectionId"] = hashlib.sha256(canonical(projection).encode()).hexdigest()[:16]
        if "message" in result:
            return {"message": self.project(result["message"], params, projection)}
        # Legacy chunks encode native JSON. Assemble within a separate bounded read
        # budget, then project once; clients never parse native message structures.
        parts, total = [], 0
        while True:
            block = base64.b64decode(result["data"])
            total += len(block)
            if total > 8 * 1024 * 1024:
                raise APIError("MESSAGE_TOO_LARGE", "Full message exceeds the 8 MiB supplemental-read budget")
            parts.append(block)
            if not result["nextCursor"]:
                break
            result = await self.workers(self.history, {**params, "cursor": result["nextCursor"]}, "message")
        return {"message": self.project(json.loads(b"".join(parts)), params, projection)}

    def manage(self, method, params):
        manager = self.manager()
        try:
            result = self._manage(manager, method, params)
            return {**result, "restartRequired": manager.restart_required,
                    "activationRequired": bool(result.get("activationRequired", False))}
        except APIError:
            raise
        except Exception as exc:
            from ....response import build_error_response
            legacy = build_error_response(exc)["error"]
            details = legacy.get("details") or {}
            safe = {key: details[key] for key in ("backup_path", "rollback_ok", "rollback_errors", "runtime_restored",
                                                 "returncode", "timed_out", "agent_name", "hook", "log_path") if key in details}
            safe.update(legacyCode=legacy["code"], restartRequired=manager.restart_required,
                        partialEffects=not isinstance(exc, (ValueError, TypeError, FileNotFoundError)) and details.get("rollback_ok") is not True)
            raise APIError("NATIVE_ERROR", "Management operation failed; inspect its rollback and protected host diagnostics", details=safe) from exc

    def _manage(self, manager, method, params):
        options = params.get("options", {})
        # CamelCase belongs to v1; snake_case stays private to retained dataclasses.
        options = {re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower(): value for key, value in options.items()}
        for field in ("workspace_root", "template_dir", "agent_zip", "skill_dir", "skill_zip"):
            value = options.get(field)
            if isinstance(value, str):
                if value == "~" or value.startswith("~/"):
                    value = str(self.context.home) + value[1:]
                    options[field] = value
                if not Path(value).is_absolute():
                    raise APIError("INVALID_ARGUMENT", f"{field} must be an absolute path in the selected environment")
        self.redact(manager, options)
        if method == "agents.create":
            options.setdefault("workspace_root", str(self.context.state_dir / "data"))
            return manager.add_agent(**options)
        if method == "dola.runtime.configure":
            return manager.configure_instance(models.CreateInstanceRequest(**options))
        if method in ("agents.refresh", "dola.agents.refresh", "agents.install"):
            name = self.store.get(params["agentId"], kind="agent", runtime=self.runtime_id)["native"]
            if method == "agents.refresh":
                if options.get("models_only") or options.get("template_only") is False:
                    raise APIError("INVALID_ARGUMENT", "Model catalog refresh belongs to dola.agents.refresh")
                options["template_only"] = True
            return manager.refresh_agent(models.RefreshAgentRequest(agent_name=name, **options)) if method.endswith("refresh") else manager.install_agent_service(name)
        if method == "skills.install":
            return manager.add_skill(models.AddSkillRequest(**options))
        simple = {"models.list": "get_supported_models", "models.get": "get_current_model", "dola.models.refresh": "update_model_catalog",
                  "connections.get": "codex_status", "connections.login": "codex_login", "connections.logout": "codex_logout",
                  "services.start": "start_agent_services"}
        if method in simple:
            return getattr(manager, simple[method])()
        if method == "models.select":
            return manager.set_model(models.SetModelRequest(params["modelId"]))
        channels = {"telegram": ("tg", models.AddTelegramBotRequest, models.DeleteTelegramBotRequest),
                    "feishu": ("feishu", models.AddFeishuBotRequest, models.DeleteFeishuBotRequest),
                    "weixin": ("weixin", models.AddWeixinBotRequest, models.DeleteWeixinBotRequest)}
        if method == "channels.list":
            return {"items": [{"channel": name, "status": getattr(manager, f"get_{entry[0]}_bot_status")()}
                               for name, entry in channels.items() if not params.get("channel") or name == params["channel"]]}
        if method in ("channels.configure", "channels.remove"):
            if params["channel"] not in channels:
                raise APIError("INVALID_ARGUMENT", "Unknown channel")
            name, add, remove = channels[params["channel"]]
            return getattr(manager, f"add_{name}_bot" if method.endswith("configure") else f"delete_{name}_bot")(
                (add if method.endswith("configure") else remove)(**options))
        raise APIError("UNSUPPORTED", "Method is not implemented by this adapter")

    async def native_call(self, method, params):
        if method == "events.subscribe":
            return await self._native_call(method, params)
        # The Gateway has no expected-session compare-and-swap. Serialize our own
        # reset/delete/send requests, then check the current segment before send.
        async with self.native_writes:
            return await self._native_call(method, params)

    async def _native_call(self, method, params):
        if not self.gateway:
            raise APIError("NOT_READY", "Native Gateway endpoint has not been configured")
        await self.gateway.connect()
        from ....core.runtime import CURRENT_WRITE_GUARD
        if CURRENT_WRITE_GUARD.get():
            CURRENT_WRITE_GUARD.get()()
        if method == "threads.create":
            agent = self.store.get(params["agentId"], kind="agent", runtime=self.runtime_id)
            response = await self.gateway.call("sessions.create", {"agentId": agent["native"]})
            key = response.get("key")
            if not isinstance(key, str) or not key.startswith("agent:" + agent["native"] + ":"):
                raise APIError("DISPATCH_UNKNOWN", "Native session creation did not return a scoped key")
            if params.get("title"):
                await self.gateway.call("sessions.patch", {"key": key, "label": params["title"]})
            identifier = self.store.object("thread", self.runtime_id, key, {"agentId": params["agentId"]})
            return {"threadId": identifier, "agentId": params["agentId"]}
        if method == "runs.cancel":
            run = self.store.get(params["runId"], kind="run", runtime=self.runtime_id)
            thread, _ = await self.workers(self.thread, run["data"]["threadId"])
            if run["data"]["status"] in ("completed", "failed", "cancelled"):
                return run["data"]
            await self.gateway.call("chat.abort", {"sessionKey": thread["native"], "runId": run["data"].get("nativeRunId", run["native"])})
            latest = self.store.get(run["id"])["data"]
            data = latest if latest["status"] in ("completed", "failed", "cancelled") else {**latest, "status": "cancel_requested"}
            self.store.object("run", self.runtime_id, run["native"], data)
            return data
        thread, entry = await self.workers(self.thread, params["threadId"])
        if method == "events.subscribe":
            await self.gateway.connect()
            return {"threadId": thread["id"], "replay": False, "resyncRequired": True}
        if method == "runs.create":
            current_segment = self.segment_id(thread["id"], entry.get("sessionId"))
            if params["expectedCurrentSegmentId"] != current_segment:
                raise APIError("SEGMENT_CHANGED", "The thread was reset; reload its current segment and preserve the draft",
                               details={"currentSegmentId": current_segment})
            if CURRENT_WRITE_GUARD.get():
                CURRENT_WRITE_GUARD.get()()
            native_id = uuid.uuid4().hex
            identifier = self.store.object("run", self.runtime_id, native_id)
            data = {"runId": identifier, "threadId": thread["id"], "clientRequestId": params["clientRequestId"],
                    "segmentId": current_segment, "operationId": params.get("_operationId"),
                    "status": "accepted", "observation": "known", "resultSync": "pending", "usageStatus": "unknown",
                    "configSnapshotId": hashlib.sha256(self.context.config_path.read_bytes()).hexdigest(), "createdAt": now_ms()}
            self.store.object("run", self.runtime_id, native_id, data)
            try:
                response = await self.gateway.call("chat.send", {"sessionKey": thread["native"], "message": params["message"],
                    "deliver": False, "idempotencyKey": native_id, **({"attachments": params["attachments"]} if "attachments" in params else {})})
            except APIError:
                self.store.object("run", self.runtime_id, native_id, {**data, "observation": "unknown"})
                raise
            if response.get("runId", native_id) != native_id:
                # Preserve our stable ID; native correlation remains explicit.
                data["nativeRunId"] = response["runId"]
                self.store.object("run_binding", self.runtime_id, response["runId"], {"runId": identifier})
            current = self.store.get(identifier, kind="run", runtime=self.runtime_id)["data"]
            if current["status"] == "accepted":
                data.update(status="running")
                self.store.object("run", self.runtime_id, native_id, data)
                return data
            return current
        native_params = {"key": thread["native"]}
        if method == "threads.update":
            native_params["label"] = params["title"]
        await self.gateway.call(NATIVE[method], native_params)
        return {"threadId": thread["id"], "applied": True}

    async def native_event(self, frame):
        event, payload = frame.get("event"), frame.get("payload") or {}
        if event == "manager.disconnected":
            for row in self.store.objects("run", self.runtime_id):
                if row["data"].get("status") in ("accepted", "running", "cancel_requested"):
                    self.store.object("run", self.runtime_id, row["native"], {**row["data"], "observation": "unknown"})
            await self.publish(self.runtime_id, None, {"type": "resync_required", "reason": "native_disconnected"})
            return
        if event not in ("chat", "agent"):
            return
        native_run = payload.get("runId")
        run = self.store.lookup("run", self.runtime_id, native_run) if isinstance(native_run, str) else None
        if run is None and isinstance(native_run, str):
            binding = self.store.lookup("run_binding", self.runtime_id, native_run)
            run = self.store.get(binding["data"]["runId"]) if binding else None
        thread = self.store.lookup("thread", self.runtime_id, payload["sessionKey"]) if isinstance(payload.get("sessionKey"), str) else None
        if thread is None and run:
            thread = self.store.get(run["data"]["threadId"], kind="thread", runtime=self.runtime_id)
        if not thread:
            return
        if event == "agent":
            data = payload.get("data") or {}
            stream = payload.get("stream")
            if stream not in ("tool", "thinking") or not isinstance(data, dict):
                return
            block_id = str(data.get("toolCallId") or "reasoning")
            identity = (str(native_run), block_id)
            previous = self.activity_revisions.get(identity, {"revision": 0, "sequence": -1})
            sequence = payload.get("seq")
            if type(sequence) is int and sequence <= previous["sequence"]:
                return
            revision = previous["revision"] + 1
            self.activity_revisions[identity] = {"revision": revision, "sequence": sequence if type(sequence) is int else previous["sequence"]}
            self.activity_revisions.move_to_end(identity)
            while len(self.activity_revisions) > 512:
                self.activity_revisions.popitem(last=False)
            if stream == "tool":
                block = {"blockId": block_id, "type": "tool_call", "toolCallId": data.get("toolCallId"),
                         "name": data.get("name", "tool"), "summary": str(data.get("meta") or "")[:180],
                         "status": "failed" if data.get("isError") else "completed" if data.get("phase") == "result" else "running"}
                mode = "snapshot"
            else:
                text = data.get("text") or data.get("delta") or ""
                if not isinstance(text, str) or len(text.encode()) > 256 * 1024:
                    await self.publish(self.runtime_id, thread["id"], {"type": "resync_required", "reason": "activity_budget"})
                    return
                block = {"blockId": block_id, "type": "reasoning", "text": text}
                mode = "snapshot" if data.get("text") else "delta"
            await self.publish(self.runtime_id, thread["id"], {"type": "run.activity", "runId": run["id"] if run else None,
                "block": block, "mode": mode, "baseRevision": previous["revision"], "revision": revision})
            return
        if event == "chat":
            state = payload.get("state")
            stream_key = str(native_run or thread["id"])
            saved = self.chat_streams.get(stream_key, {"seq": -1, "text": "", "terminal": False})
            seq = payload.get("seq")
            if type(seq) is int and seq <= saved["seq"] or saved["terminal"]:
                return
            if run:
                status = {"final": "completed", "aborted": "cancelled", "error": "failed", "delta": "running"}.get(state)
                if status:
                    self.store.object("run", self.runtime_id, run["native"], {**run["data"], "status": status,
                        "observation": "known", "resultSync": "unknown" if state in ("final", "aborted", "error") else "pending"})
            message = payload.get("message")
            if state == "delta" and not isinstance(message, dict) and isinstance(payload.get("deltaText"), str):
                text = payload["deltaText"] if payload.get("replace") else saved["text"] + payload["deltaText"]
                if len(text.encode()) > 256 * 1024:
                    await self.publish(self.runtime_id, thread["id"], {"type": "resync_required", "reason": "live_message_budget"})
                    return
                saved["text"] = text
                message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
            saved.update(seq=seq if type(seq) is int else saved["seq"], terminal=state in ("final", "aborted", "error"))
            self.chat_streams[stream_key] = saved
            self.chat_streams.move_to_end(stream_key)
            # Hard cap: 8 MiB of accumulated live text, independent of transcript size.
            while len(self.chat_streams) > 32:
                self.chat_streams.popitem(last=False)
            if isinstance(message, dict):
                # OpenClaw chat delta is an accumulated snapshot, never append it as a text delta.
                projection = {"view": "compact", "omitThinking": False, "projectionId": "live-v1"}
                native_message_id = message.get("id")
                message = {**message, "id": native_message_id or "live:" + stream_key}
                dto = message_dto(message, thread_id=thread["id"], segment_id=run["data"].get("segmentId") if run else None, projection=projection,
                                  register_resource=lambda _: None)
                dto["generationState"] = "final" if state == "final" else "streaming" if state == "delta" else "unknown"
                await self.publish(self.runtime_id, thread["id"], {"type": "message.snapshot", "message": dto,
                    "runId": run["id"] if run else None, "correlation": "native_id" if native_message_id else "unresolved"})
            if run:
                await self.publish(self.runtime_id, thread["id"], {"type": "run.updated", "run": self.store.get(run["id"])["data"]})

    async def resource_chunk(self, identifier, cursor=None, *, offset=None, length=None):
        row = self.store.get(identifier, kind="resource", runtime=self.runtime_id)
        params = {"threadId": row["data"]["threadId"], "segmentId": row["data"]["segmentId"],
                  "ref": row["data"]["reference"]["params"]["ref"]}
        if cursor:
            params["cursor"] = cursor
        if offset is not None:
            params["mediaOffset"] = offset
            params["mediaLength"] = length
        return await getattr(self, "media_workers", self.workers)(self.history, params, "media")

    async def close(self):
        if self.gateway:
            await self.gateway.close()
