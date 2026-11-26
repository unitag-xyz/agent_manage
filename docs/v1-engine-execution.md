# Engine selection and execution

One entry in `adapters.registry.ENGINES` supplies the engine's local execution factory,
version-dispatched service runtime factory, and optional trusted platform Host driver.
Only OpenClaw is registered. CLI command names are not native method names: the
parser admits public commands and `CLI_OPERATIONS` maps each to a fixed internal operation.
CLI arguments, stdin redaction, output envelopes and exit codes remain transport concerns.
The engine can normalize CLI defaults and validate native prerequisites through
`EngineDriver.validate_cli`. For example, the existing OpenClaw provisioning commands
still require their model credential; a different engine need not require it.
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

CLI operations do not require the service to be running. OpenClaw Host-bound
service adapters still refuse local management execution. A new engine may support a
subset of operations; report UNSUPPORTED rather than constructing OpenClaw as a
fallback. Static registration rejects a runtime ID already recorded under a
different engine. Use a new ID rather than reusing its durable objects and receipts.
Dynamic Host bindings keep engine-specific
ledger identities without renaming existing OpenClaw ledgers. No DSH
implementation or automatic installation is included.

## Adding an engine or a version family

1. Add `adapters/<engine>/factory.py` and register an `EngineDriver`. Supply local
   execution and/or a Runtime factory as needed. Keep its native filesystem,
   environment variables and child process setup inside the engine package;
   `RuntimeContext.environment()` provides only common HOME/XDG and trusted
   environment overrides.
2. Use `VersionFamily` rules to choose an implementation by **observed version
   and capability**. Put protocol, SDK and native storage formats in their version
   families. Use the same compatibility rules for CLI and HTTP/WSS where they
   share a capability. Local OpenClaw probes its package metadata; the private
   Host relay uses a trusted version pin and validates the actual native handshake.
3. For a verified compatible release, map a specific capability to the older
   implementation explicitly. Rules must not overlap for the same capability.
   Unknown versions must not run version-sensitive code; version-independent
   operations need not probe at all. A native exception, timeout or unknown
   outcome is **not** a reason to try another implementation. Preserve the
   original operation receipt and reconcile without resending.
4. To support shared Host, supply a platform driver for the trusted profile,
   readiness, grant resolution and native connection lifecycle. Expose
   `HostIsolation` with local roots and any namespaced exclusive native
   resources. Core still checks mount overlap and resource collisions. An
   engine with no native Gateway token need not invent one. To support inline
   Chat attachments, expose `InlineAttachments`; the service still enforces
   global frame, byte and count budgets.
5. Run the CLI, Unix HTTP/WSS and platform WSS gates. The test-only second
   engine in `tests/test_engine_extension.py` demonstrates exact selection,
   capability-specific reuse and an ambiguous write that is not retried.

OpenClaw's `execution.py` delegates offline management to the engine's application
modules. Its `v2026_7/` contains the native Gateway, transcript reader, Codex SDK
bridge, and July 1 Host reload policy. July compatibility alone never opts a
package into reload; the existing release location and source fingerprints must
also match. The old seed, container runtime and detached worker module entry
points remain compatibility wrappers for existing images and launchers.
