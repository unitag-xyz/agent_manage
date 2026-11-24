"""OpenClaw 2026.7.x adapter. Management delegates to existing execution services."""
import asyncio
import base64
import hashlib
import heapq
import json
import re
import uuid
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .... import __version__, models
from ....core.contracts import METHODS, FUTURE_METHODS
from ....core.errors import APIError
from ....core.store import canonical, now_ms
from ....local import LocalRunner
from ....orchestrator import InstanceManagerV2
from ....path_io import read_without_links
from ....segment_reader import SegmentReader, read_segment, safe_file
from ....segment_timing import measured_read
from ...base import RuntimeAdapter
from .gateway import Gateway
from .projection import message_dto

NATIVE = {"threads.create": "sessions.create", "threads.reset": "sessions.reset",
          "threads.update": "sessions.patch", "threads.models.update": "sessions.patch", "threads.delete": "sessions.delete",
          "threads.thinking.update": "sessions.patch",
          "runs.create": "chat.send", "runs.cancel": "chat.abort", "events.subscribe": "chat.send",
          "usage.status": "usage.status"}

HOST_CHAT_METHODS = {"agents.list", "chat.sidebar", "threads.list", "threads.get", "threads.create", "threads.reset",
    "threads.update", "threads.models.list", "threads.models.update", "threads.delete", "segments.list", "messages.list", "messages.get",
    "runs.create", "runs.get", "runs.lookup", "runs.cancel", "events.subscribe", "events.unsubscribe", "usage.status"}
HOST_CORE_METHODS = {"system.info", "capabilities.get", "targets.list", "environments.list",
    "runtimes.list", "runtimes.get", "operations.get", "operations.list", "operations.lookup"}
SCOPED_NATIVE_READS = {"commands.list", "tasks.list", "tasks.get", "sessions.usage",
                       "sessions.usage.timeseries", "sessions.usage.logs", "usage.cost"}
HOST_INSTANCE_ONLY = SCOPED_NATIVE_READS | {"chat.sidebar", "tasks.cancel", "sessions.subscribe", "sessions.unsubscribe",
                                           "resources.get", "threads.thinking.get", "threads.thinking.update"}
HOST_CHAT_METHODS |= HOST_INSTANCE_ONLY
NATIVE_CAPABILITIES = {**NATIVE, "threads.thinking.get": "sessions.describe",
                       **{method: method for method in (SCOPED_NATIVE_READS - {"sessions.usage.logs"})
                          | {"sessions.subscribe", "tasks.cancel"}}}


class OpenClawAdapter(RuntimeAdapter):
    engine = "openclaw"
    supported = True

    def __init__(self, registration, store, workers, publish, *, context, version, host_binding=None):
        self.registration, self.store, self.workers, self.publish = registration, store, workers, publish
        self.context, self.version = context, version
        self.runtime_id = context.runtime_id
        self.host_binding = host_binding
        config_path = context.config_path
        self.template_root = registration.get("templateRoot") or str(config_path.parent / "templates")
        self.activity_revisions = OrderedDict()
        self.chat_streams = OrderedDict()
        self.native_writes = asyncio.Lock()
        self.run_observers = {}
        self.observer_releases = set()
        device_name = self.runtime_id + ("-" + host_binding.device_scope if host_binding else "") + ".key"
        gateway_options = {"host_binding": host_binding} if host_binding else {}
        self.gateway = Gateway(registration["gatewayUrl"], config_path,
            store.path.parent / "devices" / device_name, self.native_event,
            origin=registration.get("gatewayOrigin"), expected_version=self.version,
            **gateway_options) if registration.get("gatewayUrl") else None

    def manager(self):
        if self.host_binding:
            raise APIError("UNSUPPORTED", "Host-bound Chat does not execute local management commands")
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
        config = self.host_binding.read_config() if self.host_binding else json.loads(self.context.config_path.read_text())
        if self.host_binding:
            self.host_binding.validate_agents(config)
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
            if ((self.host_binding and (method not in HOST_CHAT_METHODS | HOST_CORE_METHODS
                                        or method in HOST_INSTANCE_ONLY and not self.host_binding.instance_chat))
                    or (method == "chat.sidebar" and not self.host_binding)):
                support = "unsupported"
            readiness = "ready" if support == "adapted" else "unavailable"
            if method in NATIVE_CAPABILITIES and support == "adapted":
                readiness = "not_connected" if self.gateway else "not_configured"
                if native_methods is not None:
                    readiness = "ready"
                    if NATIVE_CAPABILITIES[method] not in native_methods:
                        support, readiness = "unsupported", "unavailable"
            methods[method] = {"support": support, "readiness": readiness}
        return {"runtimeId": self.runtime_id, "engine": "openclaw", "engineVersion": self.version,
                "adapter": "openclaw-july", "adapterVersion": __version__, "methods": methods,
                "testedVersions": [] if self.host_binding else ["2026.7.1-2"],
                "validation": "native-tested" if not self.host_binding and self.version == "2026.7.1-2" else "unverified",
                "eventReplay": False, "historyWatermark": False, "nativeProtocol": 4,
                "segmentPrecondition": "manager_serialized", "nativeSegmentCAS": False,
                "maxHistoryLimit": 200, "historyByteBudget": 1024 * 1024,
                "lifecycleOwner": self.context.lifecycle}

    def agents(self, config=None):
        if config is None:
            config = self.config()
        entries = config.get("agents", {}).get("list", [])
        if not entries:
            entries = [{"id": "main"}]
        result = []
        instance_chat = self.host_binding and self.host_binding.instance_chat
        if instance_chat and len(entries) > 64:
            raise APIError("BUSY", "Instance exceeds the Chat Agent budget")
        bindings = {item["nativeAgentId"]: item for item in self.host_binding.agents} if self.host_binding and not instance_chat else None
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            if instance_chat:
                native_id = entry["id"].lower()
                if native_id in seen or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,119}", native_id):
                    raise APIError("RUNTIME_MISMATCH", "Invalid or ambiguous native Agent ID")
                seen.add(native_id)
            binding = bindings.get(entry["id"]) if bindings is not None else None
            if bindings is not None and binding is None:
                continue
            native = (canonical(["instance-chat", native_id]) if instance_chat else
                      self.host_binding.agent_native_identity(binding) if binding else entry["id"])
            identifier = self.store.object("agent", self.runtime_id, native,
                {"name": entry.get("name", entry["id"]), "nativeAgentId": entry["id"],
                 **({"agentIdentify": binding["agentIdentify"], "installationId": binding["installationId"]} if binding else {})})
            result.append({"agentId": identifier, "runtimeId": self.runtime_id, "name": entry.get("name") or entry["id"],
                           "extensions": {"openclaw": {"agentId": entry["id"]}},
                           "isDefault": bool(entry.get("default")) or len(entries) == 1})
        return result

    def resolve_native_agent(self, native_id):
        for agent in self.agents():
            if agent["extensions"]["openclaw"]["agentId"] == native_id:
                return agent["agentId"]
        raise APIError("NOT_FOUND", "Default Agent is not installed in this runtime")

    def resolve_agent(self, identify):
        if not self.host_binding:
            raise APIError("UNSUPPORTED", "Runtime has no trusted business Agent bindings")
        for agent in self.agents():
            row = self.store.get(agent["agentId"], kind="agent", runtime=self.runtime_id)
            if row["data"].get("agentIdentify") == identify:
                return row["id"]
        raise APIError("NOT_FOUND", "Installed Agent is not registered in this runtime")

    @staticmethod
    def native_agent(row):
        return row["data"].get("nativeAgentId", row["native"])

    @staticmethod
    def native_key(row):
        return row["data"].get("nativeSessionKey", row["native"])

    def thread_identity(self, agent_id, key):
        return canonical([agent_id, key]) if self.host_binding else key

    def register_thread(self, agent_id, key):
        return self.store.object("thread", self.runtime_id, self.thread_identity(agent_id, key),
                                 {"agentId": agent_id, "nativeSessionKey": key})

    def index(self, agent_id):
        agent = self.store.get(agent_id, kind="agent", runtime=self.runtime_id)
        # Verify the agent still belongs to this runtime, not just an old registry row.
        if agent_id not in {item["agentId"] for item in self.agents()}:
            raise APIError("NOT_FOUND", "Agent is no longer configured")
        return agent, self.read_session_index(agent, self.config())

    def read_session_index(self, agent, config, max_bytes=64 * 1024 * 1024):
        root = self.context.state_dir
        configured = config.get("session", {}).get("store")
        native = self.native_agent(agent).lower()
        if configured:
            configured = configured.replace("{agentId}", native)
            path = self.host_binding.local_path(configured) if self.host_binding else Path(configured).expanduser()
        else:
            path = root / "agents" / native / "sessions/sessions.json"
        try:
            checked = safe_file(path, [path.parent])
            if self.host_binding:
                data = json.loads(read_without_links(path, max_bytes))
            else:
                with checked.open() as stream:
                    data = json.load(stream)
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise APIError("NATIVE_DATA_ERROR", "Invalid native session index")
        return data

    def sidebar_presentation(self, entry, defaults):
        result = {}
        identity = entry.get("identity")
        avatar = identity.get("avatarUrl") if isinstance(identity, dict) else None
        if (isinstance(avatar, str) and 0 < len(avatar) <= 2048
                and not any(char.isspace() or ord(char) < 32 or char == "\\" for char in avatar)):
            try:
                url = urlsplit(avatar)
                if url.scheme == "https" and url.hostname and url.username is None and url.password is None:
                    result["avatarUrl"] = avatar
            except ValueError:
                pass

        native_id = entry.get("id")
        if not isinstance(native_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", native_id):
            return result
        # Match legacy Chat's OpenClaw convention for catalog lookup, not authorization.
        if native_id.lower() != "main":
            result["templateIdentify"] = native_id
        if not self.host_binding:
            return result
        workspace = entry.get("workspace") or (defaults.get("workspace") if native_id == "main" else None)
        if not isinstance(workspace, str) or not PurePosixPath(workspace).is_absolute():
            return result
        try:
            self.host_binding.local_path(workspace)
            state_file = self.host_binding.config_path.parent / "agent-manage" / "agents" / (native_id + ".json")
            state = json.loads(read_without_links(state_file, 512 * 1024))
        except (APIError, OSError, ValueError, RecursionError):
            return result
        if (isinstance(state, dict) and type(state.get("schema_version")) is int and state["schema_version"] == 1
                and state.get("agent_name") == native_id
                and state.get("config_path") == str(self.host_binding.native_config)
                and state.get("workspace") == str(PurePosixPath(workspace))):
            template = state.get("template_name")
            if isinstance(template, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", template):
                result["templateIdentify"] = template
        return result

    @staticmethod
    def is_main_thread(config, key):
        settings = config.get("session") or {}
        configured = settings.get("mainKey")
        main_key = (configured.strip().lower() if isinstance(configured, str) else "") or "main"
        key = key.strip().lower()
        if key in ("main", main_key) or key == "global" and settings.get("scope") == "global":
            return True
        parts = key.split(":", 2)
        return len(parts) == 3 and parts[0] == "agent" and parts[2] in ("main", main_key)

    def thread_summary(self, config, key, entry):
        is_main = self.is_main_thread(config, key)
        title = next((value.strip()[:256] for field in ("displayName", "label", "derivedTitle", "lastMessagePreview")
                      if isinstance(value := entry.get(field), str) and value.strip()), None)
        updated = entry.get("updatedAt")
        return {"title": title, "updatedAt": updated if type(updated) is int and 0 <= updated <= 253402300799999 else None,
                "isMain": is_main, "canDelete": not is_main}

    def include_run_activity(self, rows):
        activity = self.store.thread_run_activity(self.runtime_id, (row["threadId"] for row in rows))
        for row in rows:
            observed = activity.get(row["threadId"], {"activeRuns": [], "lastRun": None})
            active = observed["activeRuns"]
            row["runActivity"] = {"source": "manager", **observed,
                                  "activeRunId": active[0]["runId"] if len(active) == 1 else None}

    def chat_sidebar(self, limit):
        config = self.config()
        groups = []
        agents = self.agents(config)
        agent_config = config.get("agents", {})
        entries = {entry["id"]: entry for entry in agent_config.get("list", [])
                   if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
        defaults = agent_config.get("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}

        def label(value, maximum):
            return value[:maximum] if isinstance(value, str) else None

        def timestamp(entry):
            value = entry.get("updatedAt")
            return value if type(value) is int and 0 <= value <= 253402300799999 else None

        for item in agents:
            agent_id = item["agentId"]
            agent = self.store.get(agent_id, kind="agent", runtime=self.runtime_id)
            native_id = self.native_agent(agent)
            group = {"agentId": agent_id, "name": label(item["name"], 128), "isDefault": item["isDefault"],
                     "isSystem": native_id.lower() == "main",
                     "items": [], "totalCount": None, "hasMore": False, "status": "ready",
                     **self.sidebar_presentation(entries.get(native_id, {"id": native_id}), defaults)}
            groups.append(group)
            try:
                index = self.read_session_index(agent, config, 2 * 1024 * 1024)
            except (APIError, OSError, ValueError, TypeError):
                group["status"] = "unavailable"
                continue
            prefix = "agent:" + self.native_agent(agent) + ":"
            count = 0
            def visible_rows():
                nonlocal count
                for key, entry in index.items():
                    if key.startswith(prefix) and isinstance(entry, dict):
                        count += 1
                        yield key, entry

            rows = heapq.nlargest(limit + 1,
                visible_rows(),
                key=lambda pair: (timestamp(pair[1]) or 0, pair[0]))
            group["totalCount"] = count
            group["hasMore"] = len(rows) > limit
            for key, entry in rows[:limit]:
                group["items"].append({"threadId": self.register_thread(agent_id, key),
                    **self.thread_summary(config, key, entry)})
        self.include_run_activity([row for group in groups for row in group["items"]])
        return {"items": groups}

    def threads(self, agent_id):
        agent, index = self.index(agent_id)
        config = self.config()
        result = []
        for key, entry in index.items():
            if not key.startswith("agent:" + self.native_agent(agent) + ":") or not isinstance(entry, dict):
                continue
            identifier = self.register_thread(agent_id, key)
            result.append({"threadId": identifier, "agentId": agent_id, **self.thread_summary(config, key, entry),
                           "currentSegmentId": self.segment_id(identifier, entry.get("sessionId"))})
        self.include_run_activity(result)
        return result

    def thread(self, identifier):
        row = self.store.get(identifier, kind="thread", runtime=self.runtime_id)
        _, index = self.index(row["data"]["agentId"])
        entry = index.get(self.native_key(row))
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
            if self.host_binding and params["agentId"] not in {item["agentId"] for item in self.agents()}:
                raise APIError("NOT_FOUND", "Installed Agent binding is no longer active")
            return params["agentId"]
        if "threadId" in params:
            agent_id = self.store.get(params["threadId"], kind="thread", runtime=self.runtime_id)["data"]["agentId"]
            return self.resolve_scope(method, {"agentId": agent_id})
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
        payload = {"root": str(self.context.state_dir), "config": self.config(), "sessionKey": self.native_key(thread),
                   "sessionId": segment["data"]["sessionId"], "action": action, "includeSnapshot": True,
                   "includeMessageStatistics": action in ("history", "message"),
                   "maxTranscriptBytes": 256 * 1024 * 1024, "maxRecordBytes": 16 * 1024 * 1024, "maxRecords": 20000}
        if self.host_binding:
            payload["_nativePathResolver"] = self.host_binding.local_path
            payload["scopeBinding"] = thread["data"]["agentId"]
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
        dto["statistics"] = message.get("statistics")
        if message.get("fullMessageRef"):
            dto["fullMessageRef"] = {"method": "messages.get", "params": {"runtimeId": self.runtime_id,
                "threadId": params["threadId"], "segmentId": params["segmentId"], "messageId": message["id"],
                "ref": message["fullMessageRef"]["params"]["ref"]}}
        return dto

    @staticmethod
    def project_usage_status(payload):
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, list) or len(providers) > 64:
            raise APIError("NATIVE_DATA_ERROR", "Invalid native usage status")
        result = []
        for provider in providers:
            name = provider.get("provider") if isinstance(provider, dict) else None
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                continue
            row = {"provider": name, "windows": []}
            result.append(row)
            if provider.get("error"):
                row["error"] = "Usage unavailable"
                continue
            email = provider.get("accountEmail")
            if isinstance(email, str) and len(email) <= 254 and re.fullmatch(r"[^\s@]+@[^\s@]+", email):
                row["accountEmail"] = email
            windows = provider.get("windows")
            if not isinstance(windows, list) or len(windows) > 32:
                row["error"] = "Usage unavailable"
                continue
            for window in windows:
                if not isinstance(window, dict):
                    continue
                label, used, reset = (window.get(key) for key in ("label", "usedPercent", "resetAt"))
                if (not isinstance(label, str) or not label.strip() or len(label) > 64
                        or type(used) not in (int, float) or not 0 <= used <= 100):
                    continue
                value = {"label": label, "usedPercent": used}
                if type(reset) in (int, float) and 0 < reset <= 253402300799999:
                    value["resetAt"] = reset
                row["windows"].append(value)
        return {"providers": result}

    async def scoped_native(self, method, params):
        if method == "sessions.usage.logs":
            return await self.workers(self.thread_usage_logs, params)
        if not self.gateway:
            raise APIError("NOT_READY", "Native Gateway endpoint has not been configured")
        if method == "usage.cost":
            data = await self.gateway.call(method, {"agentScope": "all", "range": params.get("range", "30d")})
            return self.usage_summary(data, ("updatedAt", "startDate", "endDate", "daily", "totals", "cacheStatus"))
        thread, _ = await self.workers(self.thread, params["threadId"])
        key = self.native_key(thread)
        agent = self.store.get(thread["data"]["agentId"], kind="agent", runtime=self.runtime_id)
        agent_id = self.native_agent(agent)
        if method == "commands.list":
            data = await self.gateway.call(method, {"agentId": agent_id, "scope": "text", "includeArgs": False})
            return self.usage_summary(data, ("commands",))
        if method == "tasks.list":
            data = await self.gateway.call(method, {"sessionKey": key, "limit": params.get("limit", 50),
                **{field: params[field] for field in ("cursor", "status") if field in params}})
            if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
                raise APIError("NATIVE_DATA_ERROR", "Invalid native task list")
            return {**({"nextCursor": data["nextCursor"]} if isinstance(data.get("nextCursor"), str)
                       and re.fullmatch(r"[0-9]{1,12}", data["nextCursor"]) else {}),
                    "tasks": [self.task_summary(task) for task in data["tasks"][:params.get("limit", 50)]
                              if self.task_matches(task, key)]}
        if method in ("tasks.get", "tasks.cancel"):
            data = await self.gateway.call("tasks.get", {"taskId": params["taskId"]})
            task = data.get("task") if isinstance(data, dict) else None
            if not self.task_matches(task, key) or (task.get("taskId") or task.get("id")) != params["taskId"]:
                raise APIError("NOT_FOUND", "Task not found in this thread")
            if method == "tasks.get":
                return {"task": self.task_summary(task)}
            from ....core.runtime import CURRENT_WRITE_GUARD
            if CURRENT_WRITE_GUARD.get():
                CURRENT_WRITE_GUARD.get()()
            data = await self.gateway.call(method, {"taskId": params["taskId"]})
            if not isinstance(data, dict) or type(data.get("cancelled")) is not bool:
                raise APIError("DISPATCH_UNKNOWN", "Native Gateway did not confirm task cancellation")
            return {"cancelled": data["cancelled"]}
        if method == "sessions.usage":
            data = await self.gateway.call(method, {"key": key, "agentId": agent_id, "limit": 1,
                "groupBy": "family", "range": params.get("range", "30d")})
            result = self.usage_summary(data, ("updatedAt", "startDate", "endDate", "totals", "aggregates", "cacheStatus"))
        else:
            data = await self.gateway.call(method, {"key": key})
            result = self.usage_summary(data, ("points",))
        return {"threadId": thread["id"], **result}

    def thread_usage_logs(self, params):
        thread_id = params["threadId"]
        thread, entry = self.thread(thread_id)
        segment_id = params.get("segmentId") or self.segment_id(thread_id, entry.get("sessionId"))
        if not segment_id:
            return {"threadId": thread_id, "segmentId": None, "logs": []}
        result = self.history({"threadId": thread_id, "segmentId": segment_id,
                               "limit": params.get("limit", 50)}, "usage")
        for row in result["logs"]:
            if native_run := row.pop("runId", None):
                run = self.store.lookup("run", self.runtime_id, native_run)
                if run is None:
                    binding = self.store.lookup("run_binding", self.runtime_id, native_run)
                    if binding:
                        run = self.store.get(binding["data"]["runId"], kind="run", runtime=self.runtime_id)
                if run and run["data"].get("threadId") == thread_id and run["data"].get("segmentId") == segment_id:
                    row["runId"] = run["id"]
                    boundary = run["data"].get("resultBoundary") or {}
                    row["isFinalAssistantMessage"] = (row["role"] == "assistant"
                        and boundary.get("candidateAssistantMessageId") == row["messageId"])
        return {"threadId": thread["id"], "segmentId": segment_id, "logs": result["logs"]}

    @staticmethod
    def usage_summary(data, keys):
        if not isinstance(data, dict):
            raise APIError("NATIVE_DATA_ERROR", "Invalid native response")
        result = {key: data[key] for key in keys if key in data}
        if len(canonical(result).encode()) > 512 * 1024:
            raise APIError("RESPONSE_TOO_LARGE", "Native response exceeds the Chat budget")
        return result

    @staticmethod
    def task_matches(task, key):
        return isinstance(task, dict) and key in (task.get("sessionKey"), task.get("childSessionKey"), task.get("ownerKey"))

    @staticmethod
    def task_summary(task):
        return {key: value[:256] if isinstance(value, str) else value for key, value in task.items()
                if key in ("id", "taskId", "kind", "status", "title", "createdAt", "updatedAt", "startedAt", "endedAt")
                and type(value) in (str, int)}

    async def invoke(self, method, params):
        if self.host_binding:
            if (method not in HOST_CHAT_METHODS
                    or not self.host_binding.instance_chat and (method in HOST_INSTANCE_ONLY
                                                                or method == "runs.create" and params.get("attachments"))):
                raise APIError("UNSUPPORTED", "Method is not available for Host Chat")
            self.resolve_scope(method, params)
        if method in SCOPED_NATIVE_READS or method == "tasks.cancel":
            return await self.scoped_native(method, params)
        if method in ("sessions.subscribe", "sessions.unsubscribe"):
            if method == "sessions.unsubscribe":
                return {"subscribed": False}
            if not self.gateway:
                raise APIError("NOT_READY", "Native Gateway endpoint has not been configured")
            result = await self.gateway.call(method, {})
            if not isinstance(result, dict) or result.get("subscribed") is not True:
                raise APIError("NOT_READY", "Native Gateway did not confirm session subscription")
            return {"subscribed": True, "replay": False}
        if method == "threads.models.list":
            thread, entry = await self.workers(self.thread, params["threadId"])
            if not self.gateway:
                raise APIError("NOT_READY", "Native Gateway endpoint has not been configured")
            data = await self.gateway.call("sessions.describe", {"key": self.native_key(thread)})
            if not isinstance(data.get("session"), dict):
                raise APIError("NOT_FOUND", "Thread no longer exists")
            model = self.model_reference(data.get("resolved") or data["session"])
            return {"threadId": thread["id"], "items": list((await self.configured_thread_models()).values()),
                    "modelId": model, "usesDefault": not bool(entry.get("modelOverride")), "canRestoreDefault": True}
        if method == "threads.thinking.get":
            thread, entry = await self.workers(self.thread, params["threadId"])
            return await self.thread_thinking(thread, entry)
        if method == "usage.status":
            if not self.gateway:
                raise APIError("NOT_READY", "Native Gateway endpoint has not been configured")
            return self.project_usage_status(await self.gateway.call("usage.status", {}, timeout=15))
        if method in NATIVE:
            return await self.native_call(method, params)
        if method == "agents.list":
            return {"items": await self.workers(self.agents)}
        if method == "chat.sidebar":
            if not (self.host_binding and self.host_binding.instance_chat):
                raise APIError("UNSUPPORTED", "Instance Chat is required for the sidebar")
            limit = params.get("limit", 5)
            if not 1 <= limit <= 20:
                raise APIError("INVALID_ARGUMENT", "Sidebar limit must be between 1 and 20")
            return await self.workers(self.chat_sidebar, limit)
        if method == "threads.list":
            return {"items": await self.workers(self.threads, params["agentId"])}
        if method in ("threads.get", "segments.list"):
            thread, entry = await self.workers(self.thread, params["threadId"])
            if method == "threads.get":
                def summarize():
                    row = {"threadId": thread["id"], **self.thread_summary(self.config(), self.native_key(thread), entry)}
                    self.include_run_activity([row])
                    return row
                summary = await self.workers(summarize)
                return {"threadId": thread["id"], "agentId": thread["data"]["agentId"], **summary,
                        **{key: entry[key] for key in ("totalTokens", "contextTokens")
                           if type(entry.get(key)) is int and 0 <= entry[key] <= (1 << 53) - 1},
                        **({"totalTokensFresh": entry["totalTokensFresh"]}
                           if type(entry.get("totalTokensFresh")) is bool else {}),
                        "currentSegmentId": self.segment_id(thread["id"], entry.get("sessionId"))}
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
            if "offset" in params:
                chunk = await self.resource_chunk(row["id"], offset=params["offset"], length=params["length"], host_ws=True)
                return {key: chunk[key] for key in ("contentType", "totalBytes", "sha256", "offset", "encoding", "data") if key in chunk}
            reference = row["data"]["reference"]
            return {"resourceId": row["id"], "threadId": row["data"]["threadId"], "role": row["data"]["role"],
                "availability": "unknown", "bytes": reference.get("sizeBytes"),
                **{name: reference[name] for name in ("purpose", "fileName", "mimeType", "sizeBytes") if name in reference},
                **({"url": f"/v1/resources/{row['id']}?runtimeId={self.runtime_id}"} if not self.host_binding else {})}
        if method in ("runs.get", "runs.lookup"):
            if method == "runs.get":
                row = self.store.get(params["runId"], kind="run", runtime=self.runtime_id)
                return await self.refresh_run_outcome(row)
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

    @staticmethod
    def model_reference(model_info):
        model = model_info.get("model")
        provider = model_info.get("modelProvider")
        if not isinstance(model, str) or not model.strip():
            return None
        return (f"{provider}/{model}" if isinstance(provider, str) and provider.strip()
                and not model.lower().startswith(provider.lower() + "/") else model)

    async def configured_thread_models(self):
        catalog = (await self.gateway.call("models.list", {"view": "configured"})).get("models")
        if not isinstance(catalog, list):
            raise APIError("NATIVE_DATA_ERROR", "Native configured model catalog is invalid")
        if len(catalog) > 512:
            raise APIError("BUSY", "Configured model catalog exceeds Chat budget")
        result = {}
        for item in catalog:
            if not isinstance(item, dict) or not isinstance(item.get("provider"), str) or not item["provider"].strip():
                continue
            reference = self.model_reference({"model": item.get("id"), "modelProvider": item["provider"]})
            if not reference or len(reference) > 256:
                continue
            name = item.get("name")
            result[reference] = {"modelId": reference, "name": name[:256] if isinstance(name, str) and name.strip() else reference,
                **({"available": item["available"]} if type(item.get("available")) is bool else {})}
        return result

    async def update_thread_model(self, thread, entry, model, write_guard):
        if model is not None and model not in await self.configured_thread_models():
            raise APIError("INVALID_ARGUMENT", "Model is not in the configured model catalog")
        key = self.native_key(thread)
        await self.require_idle_thread(thread, entry)
        if write_guard:
            write_guard()
        result = await self.gateway.call("sessions.patch", {"key": key, "model": model})
        resolved = result.get("resolved")
        effective = self.model_reference(resolved) if isinstance(resolved, dict) else None
        updated = result.get("entry")
        if (result.get("key") != key or not effective or not isinstance(updated, dict)
                or bool(updated.get("modelOverride")) != (model is not None)
                or (model is not None and effective != model)):
            raise APIError("DISPATCH_UNKNOWN", "Native Gateway did not confirm the requested model change")
        return {"threadId": thread["id"], "modelId": effective, "usesDefault": model is None, "applied": True}

    async def require_idle_thread(self, thread, entry):
        key = self.native_key(thread)
        agent = self.store.get(thread["data"]["agentId"], kind="agent", runtime=self.runtime_id)
        data = await self.gateway.call("sessions.list", {"agentId": self.native_agent(agent), "search": key, "limit": 16,
            **({"archived": True} if entry.get("archivedAt") is not None else {})})
        sessions = data.get("sessions")
        matching = [item for item in sessions if isinstance(item, dict) and item.get("key") == key] if isinstance(sessions, list) else []
        if len(matching) != 1 or type(matching[0].get("hasActiveRun")) is not bool:
            raise APIError("NOT_READY", "Could not confirm that the target thread is idle", retryable=True)
        if matching[0]["hasActiveRun"]:
            raise APIError("THREAD_BUSY", "Cannot change session settings while the thread is generating", retryable=True)

    @staticmethod
    def thinking_level(value):
        return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9]{1,32}", value) else None

    async def thread_thinking(self, thread, entry):
        if not self.gateway:
            raise APIError("NOT_READY", "Native Gateway endpoint has not been configured")
        result = await self.gateway.call("sessions.describe", {"key": self.native_key(thread)})
        session = result.get("session") if isinstance(result, dict) else None
        if not isinstance(session, dict):
            raise APIError("NOT_FOUND", "Thread no longer exists")
        available = session.get("thinkingLevels")
        levels = []
        seen = set()
        for item in available[:32] if isinstance(available, list) else []:
            identifier = self.thinking_level(item.get("id")) if isinstance(item, dict) else None
            if identifier and identifier not in seen:
                seen.add(identifier)
                label = item.get("label")
                levels.append({"id": identifier, **({"label": label[:64]} if isinstance(label, str) else {})})
        return {"threadId": thread["id"], "modelId": self.model_reference(session),
                "thinkingLevel": self.thinking_level(entry.get("thinkingLevel")),
                "thinkingDefault": self.thinking_level(session.get("thinkingDefault")),
                "thinkingLevels": levels, "supported": bool(levels)}

    async def update_thread_thinking(self, thread, entry, level, write_guard):
        profile = await self.thread_thinking(thread, entry)
        if not profile["supported"]:
            raise APIError("UNSUPPORTED", "The native model did not advertise thinking levels")
        if level is not None and level not in {item["id"] for item in profile["thinkingLevels"]}:
            raise APIError("INVALID_ARGUMENT", "Thinking level is not supported by this thread's model")
        await self.require_idle_thread(thread, entry)
        if write_guard:
            write_guard()
        key = self.native_key(thread)
        result = await self.gateway.call("sessions.patch", {"key": key, "thinkingLevel": level})
        updated = result.get("entry")
        if result.get("key") != key or not isinstance(updated, dict) or self.thinking_level(updated.get("thinkingLevel")) != level:
            raise APIError("DISPATCH_UNKNOWN", "Native Gateway did not confirm the thinking level change")
        return {"threadId": thread["id"], "thinkingLevel": level, "applied": True}

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
            response = await self.gateway.call("sessions.create", {"agentId": self.native_agent(agent)})
            key = response.get("key")
            if not isinstance(key, str) or not key.startswith("agent:" + self.native_agent(agent) + ":"):
                raise APIError("DISPATCH_UNKNOWN", "Native session creation did not return a scoped key")
            if params.get("title"):
                await self.gateway.call("sessions.patch", {"key": key, "label": params["title"]})
            identifier = self.register_thread(params["agentId"], key)
            return {"threadId": identifier, "agentId": params["agentId"]}
        if method == "runs.cancel":
            run = self.store.get(params["runId"], kind="run", runtime=self.runtime_id)
            thread, _ = await self.workers(self.thread, run["data"]["threadId"])
            if run["data"]["status"] in ("completed", "failed", "cancelled"):
                return run["data"]
            await self.gateway.call("chat.abort", {"sessionKey": self.native_key(thread), "runId": run["data"].get("nativeRunId", run["native"])})
            latest = self.store.get(run["id"])["data"]
            data = latest if latest["status"] in ("completed", "failed", "cancelled") else {**latest, "status": "cancel_requested"}
            self.store.object("run", self.runtime_id, run["native"], data)
            return data
        thread, entry = await self.workers(self.thread, params["threadId"])
        if method == "threads.models.update":
            return await self.update_thread_model(thread, entry, params["modelId"], CURRENT_WRITE_GUARD.get())
        if method == "threads.thinking.update":
            return await self.update_thread_thinking(thread, entry, params["thinkingLevel"], CURRENT_WRITE_GUARD.get())
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
            native_key = self.native_key(thread)
            observer = None
            if self.host_binding:
                if len(self.run_observers) >= 128:
                    raise APIError("BUSY", "Native run observation limit reached", retryable=True)
                agent = self.store.get(thread["data"]["agentId"], kind="agent", runtime=self.runtime_id)
                observer = await self.gateway.retain_session(native_key, self.native_agent(agent))
                self.run_observers[native_id] = observer
                try:
                    if CURRENT_WRITE_GUARD.get():
                        CURRENT_WRITE_GUARD.get()()
                except BaseException:
                    self.release_run_observer(native_id)
                    raise
            identifier = self.store.object("run", self.runtime_id, native_id)
            data = {"runId": identifier, "threadId": thread["id"], "clientRequestId": params["clientRequestId"],
                    "segmentId": current_segment, "operationId": params.get("_operationId"),
                    "status": "accepted", "observation": "known", "resultSync": "pending", "usageStatus": "unknown",
                    "configSnapshotId": hashlib.sha256(self.host_binding.config_bytes() if self.host_binding else self.context.config_path.read_bytes()).hexdigest(), "createdAt": now_ms()}
            self.store.object("run", self.runtime_id, native_id, data)
            try:
                response = await self.gateway.call("chat.send", {"sessionKey": native_key, "message": params["message"],
                    "deliver": False, "idempotencyKey": native_id, **({"attachments": params["attachments"]} if "attachments" in params else {})},
                    **({"expected_socket": observer[0]} if observer is not None else {}))
            except APIError as exc:
                if exc.code != "DISPATCH_UNKNOWN":
                    self.release_run_observer(native_id)
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
        native_params = {"key": self.native_key(thread)}
        if method == "threads.update":
            native_params["label"] = params["title"]
        elif method == "threads.delete":
            if await self.workers(lambda: self.is_main_thread(self.config(), self.native_key(thread))):
                raise APIError("MAIN_THREAD_PROTECTED", "The main thread cannot be deleted")
            if CURRENT_WRITE_GUARD.get():
                CURRENT_WRITE_GUARD.get()()
            native_params["deleteTranscript"] = True
        await self.gateway.call(NATIVE[method], native_params)
        return {"threadId": thread["id"], "applied": True}

    def release_run_observer(self, native_id):
        if (observer := self.run_observers.pop(native_id, None)) is not None:
            # The native reader must stay free to receive the unsubscribe response.
            task = asyncio.create_task(self.gateway.release_session(observer))
            self.observer_releases.add(task)
            task.add_done_callback(self.observer_releases.discard)

    async def refresh_run_outcome(self, row):
        data = row["data"]
        active = ("accepted", "running", "cancel_requested")
        if not self.host_binding or self.gateway is None or data.get("status") not in active:
            return data
        native = data.get("nativeRunId") or row["native"]
        # This is observation of an exact native run, never a second chat.send.
        try:
            outcome = await self.gateway.call("agent.wait", {"runId": native, "timeoutMs": 1}, timeout=2)
        except APIError:
            return self.store.get(row["id"], kind="run", runtime=self.runtime_id)["data"]
        current = self.store.get(row["id"], kind="run", runtime=self.runtime_id)["data"]
        if (current.get("status") not in active or not isinstance(outcome, dict)
                or outcome.get("status") != "ok" or outcome.get("runId") != native
                or type(outcome.get("endedAt")) is not int):
            return current
        current = {**current, "status": "completed", "observation": "known", "resultSync": "unknown",
                   "completedAt": outcome["endedAt"],
                   "resultBoundary": {"segmentId": current.get("segmentId"), "status": "unverified"}}
        self.store.object("run", self.runtime_id, row["native"], current)
        self.release_run_observer(row["native"])
        await self.publish(self.runtime_id, current["threadId"], {"type": "run.updated", "run": current})
        return current

    async def native_event(self, frame):
        event, payload = frame.get("event"), frame.get("payload") or {}
        if event == "manager.disconnected":
            self.run_observers.clear()
            for row in self.store.objects("run", self.runtime_id):
                if row["data"].get("status") in ("accepted", "running", "cancel_requested"):
                    self.store.object("run", self.runtime_id, row["native"], {**row["data"], "observation": "unknown"})
            await self.publish(self.runtime_id, None, {"type": "resync_required", "reason": "native_disconnected"})
            return
        if event == "sessions.changed":
            await self.publish(self.runtime_id, None, {"type": "sessions.changed"})
            return
        if event not in ("chat", "agent"):
            return
        native_run = payload.get("runId")
        run = self.store.lookup("run", self.runtime_id, native_run) if isinstance(native_run, str) else None
        if run is None and isinstance(native_run, str):
            binding = self.store.lookup("run_binding", self.runtime_id, native_run)
            run = self.store.get(binding["data"]["runId"]) if binding else None
        key = payload.get("sessionKey")
        thread = None
        if isinstance(key, str):
            if self.host_binding:
                for item in self.agents():
                    row = self.store.get(item["agentId"])
                    if key.startswith("agent:" + self.native_agent(row) + ":"):
                        thread = self.store.lookup("thread", self.runtime_id, self.thread_identity(row["id"], key))
                        break
            else:
                thread = self.store.lookup("thread", self.runtime_id, key)
        if thread is None and run:
            thread = self.store.get(run["data"]["threadId"], kind="thread", runtime=self.runtime_id)
        if not thread:
            return
        if self.host_binding:
            try:
                self.resolve_scope("events.subscribe", {"threadId": thread["id"]})
            except APIError:
                return
            if run and run["data"]["threadId"] != thread["id"]:
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
            terminal = state in ("final", "aborted", "error")
            # OpenClaw can emit the terminal frame from a different sequence counter.
            if saved["terminal"] or (not terminal and type(seq) is int and seq <= saved["seq"]):
                return
            if run:
                status = {"final": "completed", "aborted": "cancelled", "error": "failed", "delta": "running"}.get(state)
                if state == "delta" and run["data"].get("status") in ("completed", "cancelled", "failed"):
                    return
                if status:
                    message = payload.get("message")
                    event_id = message.get("id") if state == "final" and isinstance(message, dict) and message.get("role") == "assistant" else None
                    boundary = {"segmentId": run["data"].get("segmentId"), "status": "unverified"}
                    if isinstance(event_id, str) and 0 < len(event_id) <= 256 and event_id.isprintable():
                        boundary["candidateAssistantMessageId"] = event_id
                    self.store.object("run", self.runtime_id, run["native"], {**run["data"], "status": status,
                        **({"completedAt": now_ms(), "resultBoundary": boundary} if terminal else {}),
                        **({"error": {"code": "RUN_FAILED", "message": "Generation failed", "retryable": False}}
                           if state == "error" else {}),
                        "observation": "known", "resultSync": "unknown" if state in ("final", "aborted", "error") else "pending"})
                    if terminal:
                        self.release_run_observer(run["native"])
            # Native error frames can embed the unsanitized provider diagnostic in message.content.
            message = payload.get("message") if state != "error" else None
            if state == "delta" and not isinstance(message, dict) and isinstance(payload.get("deltaText"), str):
                text = payload["deltaText"] if payload.get("replace") else saved["text"] + payload["deltaText"]
                if len(text.encode()) > 256 * 1024:
                    await self.publish(self.runtime_id, thread["id"], {"type": "resync_required", "reason": "live_message_budget"})
                    return
                saved["text"] = text
                message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
            saved.update(seq=seq if type(seq) is int else saved["seq"], terminal=terminal)
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

    async def resource_chunk(self, identifier, cursor=None, *, offset=None, length=None, host_ws=False):
        if self.host_binding and not host_ws:
            raise APIError("UNSUPPORTED", "Host-bound media is available only through the Chat resource RPC")
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
        if self.observer_releases:
            await asyncio.gather(*self.observer_releases, return_exceptions=True)
