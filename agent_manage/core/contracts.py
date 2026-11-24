"""The v1 method registry is both dispatch policy and discoverable contract."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Method:
    write: bool = False
    scope: str = "runtime"
    required: tuple = ()
    optional: tuple = ()


METHODS = {
    "system.info": Method(scope="system"),
    "capabilities.get": Method(scope="discovery"),
    "targets.list": Method(scope="system"),
    "environments.list": Method(scope="system"),
    "runtimes.list": Method(scope="system"),
    "runtimes.get": Method(scope="discovery"),
    "runtimes.recover": Method(True),
    "agents.list": Method(scope="collection"),
    "chat.sidebar": Method(scope="collection", optional=("limit",)),
    "agents.create": Method(True, required=("options",)),
    "agents.refresh": Method(True, "runtime", ("agentId",), ("options",)),
    "agents.install": Method(True, "runtime", ("agentId",)),
    "dola.runtime.configure": Method(True, required=("options",)),
    "dola.agents.refresh": Method(True, "runtime", ("agentId",), ("options",)),
    "skills.install": Method(True, required=("options",)),
    "models.list": Method(),
    "models.get": Method(),
    "usage.status": Method(),
    "usage.cost": Method(optional=("range",)),
    "sessions.usage": Method(False, "thread", ("threadId",), ("range",)),
    "sessions.usage.timeseries": Method(False, "thread", ("threadId",)),
    "sessions.usage.logs": Method(False, "thread", ("threadId",), ("segmentId", "limit")),
    "commands.list": Method(False, "thread", ("threadId",)),
    "tasks.list": Method(False, "thread", ("threadId",), ("limit", "cursor", "status")),
    "tasks.get": Method(False, "thread", ("threadId", "taskId")),
    "tasks.cancel": Method(True, "thread", ("threadId", "taskId")),
    "models.select": Method(True, required=("modelId",)),
    "dola.models.refresh": Method(True),
    "channels.list": Method(optional=("channel",)),
    "channels.configure": Method(True, required=("channel", "options")),
    "channels.remove": Method(True, required=("channel", "options")),
    "connections.get": Method(),
    "connections.login": Method(True),
    "connections.logout": Method(True),
    "services.start": Method(True),
    "threads.list": Method(False, "agent", ("agentId",)),
    "threads.get": Method(False, "thread", ("threadId",)),
    "threads.create": Method(True, "agent", ("agentId",), ("title",)),
    "threads.reset": Method(True, "thread", ("threadId",)),
    "threads.update": Method(True, "thread", ("threadId", "title")),
    "threads.models.list": Method(False, "thread", ("threadId",)),
    "threads.models.update": Method(True, "thread", ("threadId", "modelId")),
    "threads.thinking.get": Method(False, "thread", ("threadId",)),
    "threads.thinking.update": Method(True, "thread", ("threadId", "thinkingLevel")),
    "threads.delete": Method(True, "thread", ("threadId",)),
    "segments.list": Method(False, "thread", ("threadId",)),
    "messages.list": Method(False, "thread", ("threadId", "segmentId"), ("edge", "limit", "cursor", "view", "omitThinking")),
    "messages.get": Method(False, "thread", ("threadId", "segmentId", "messageId"), ("ref",)),
    "resources.get": Method(False, "resource", ("resourceId",), ("offset", "length")),
    "runs.create": Method(True, "thread", ("threadId", "message", "expectedCurrentSegmentId"), ("attachments",)),
    "runs.get": Method(False, "run", ("runId",)),
    "runs.lookup": Method(False, "thread", ("threadId", "clientRequestId")),
    "runs.cancel": Method(True, "run", ("runId",)),
    "operations.get": Method(scope="operation", required=("operationId",)),
    "operations.list": Method(scope="owned_operations"),
    "operations.lookup": Method(scope="owned_operations", required=("clientRequestId",),
                                optional=("threadId", "operationMethod")),
    "events.subscribe": Method(False, "thread", ("threadId",), ("afterSequence",)),
    "events.unsubscribe": Method(False, "thread", ("threadId",)),
    "sessions.subscribe": Method(),
    "sessions.unsubscribe": Method(),
}

INSTANCE_CHAT_ADDITIONS = frozenset({
    "capabilities.get", "chat.sidebar", "threads.models.list", "threads.models.update", "operations.lookup",
    "threads.thinking.get", "threads.thinking.update",
    "usage.status", "usage.cost", "sessions.usage", "sessions.usage.timeseries", "sessions.usage.logs",
    "commands.list", "tasks.list", "tasks.get", "tasks.cancel", "threads.delete", "threads.reset",
    "sessions.subscribe", "sessions.unsubscribe", "resources.get",
})

FUTURE_METHODS = ("targets.register", "environments.create", "runtimes.install", "projects.list",
                  "memories.list", "approvals.resolve", "queues.enqueue", "usage.get",
                  "artifacts.register", "dola.shares.create", "dola.sites.register")


def schema():
    return {name: {**asdict(spec), "required": list(spec.required), "optional": list(spec.optional)}
            for name, spec in METHODS.items()}
