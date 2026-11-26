# Engine selection and execution

One entry in `adapters.registry.ENGINES` supplies the engine's local execution factory,
version-dispatched service runtime factory, and optional trusted platform Host driver.
Only OpenClaw is registered. CLI command names are not native method names: the
parser admits public commands and `CLI_OPERATIONS` maps each to a fixed internal operation.
CLI arguments, stdin redaction, output envelopes and exit codes remain transport concerns.
The v1 transport still applies its own capabilities, authorization, operation receipts,
and native ID resolution. Do not expose local commands as arbitrary RPC methods.

| Operations | Existing CLI | v1 route and boundaries |
| --- | --- | --- |
| Provisioning | create-instance, configure-instance, activate-instance, add-agent, add-agents | agents.create and dola.runtime.configure use shared execution; no new RPC for batch creation or Fly activation |
| Installation and refresh | agent-install, agents-start, add-skill, refresh-agent | agents.install, services.start, skills.install, agents.refresh and dola.agents.refresh; agents.refresh stays template-only |
| Models and accounts | models, current-model, set-model, update-model, codex-login/logout/status | models.list/get/select, dola.models.refresh, connections.login/logout/get |
| Channels | add/delete/status for Telegram, Feishu and Weixin | channels.configure/remove/list retain per-channel validation |
| Inspection and conversation | agents-list, check-server-status, current-gateway-token, sessions-segments-history/message-get/media-get | agents.list, messages and resources use scoped IDs and projections; no new remote diagnostics or token access |
| Native live chat | no CLI equivalent required | versioned runtime adapter owns threads, runs, events and native recovery |
| Manager or parser only | --help, --version | grants, leases and operation receipts are not native operations |

## Local deployment identity

With no new settings, existing CLI and Host deployments select OpenClaw. For
bootstrap, the trusted launcher may set `UNITAG_AGENT_MANAGER_ENGINE`. Unknown
or empty values fail closed; CLI does not inspect tenant-owned native configs or
start a Gateway to detect the engine. Native commands keep their own prerequisites.

For an existing shared-Host container the launcher can mount **only this workload's**
root-owned binding directory read-only and set `UNITAG_AGENT_MANAGER_BINDING_FILE`
to its absolute `<instanceIdentify>.json` path and `UNITAG_AGENT_MANAGER_BINDING_ID`
to its assigned generation, in container mode. Publish replacements atomically;
mount the directory, not a stale single-file inode. Both values must be supplied
together. Manager rereads the bounded record on each CLI invocation, checks its
ownership, ancestor permissions, catalog schema and pinned generation. A conflicting
bootstrap engine, invalid binding, or path override fails before execution. The CLI
runs inside the assigned workload; it does not follow the Host mountRoot into
another namespace. These settings are trusted launcher inputs, never request options
or a substitute for Host authorization. For deployments that don't supply them
there is no independent CLI check against a Host catalog.

CLI operations do not require the service to be running. Host-bound service
adapters still refuse local management execution. A new engine may support a
subset of operations; report UNSUPPORTED rather than constructing OpenClaw as a
fallback. Use a new static runtime ID when changing its engine; static runtime IDs
name durable objects and receipts. Dynamic Host bindings keep engine-specific
ledger identities without renaming existing OpenClaw ledgers. No DSH
implementation or automatic installation is included.
