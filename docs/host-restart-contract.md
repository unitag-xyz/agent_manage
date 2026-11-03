# Host extra-restart decision

`restartRequired` means the caller still needs to restart the runtime. It does
not describe an internal restart that OpenClaw performs during automatic config
reload, and `false` is not a synchronous configuration-applied acknowledgment.

The optimization is limited to `create-instance`, `configure-instance`, and the
`add-agent` / `add-agents` aggregate in the standard Host installation beneath
`/opt/unitag-agent-manager`. The resolved AM module path must remain under that
mount. Custom installation paths and Fly's `/opt/unitag/agent-manager` retain
the old behavior. An activation-mode environment setting also disables this
optimization, keeping `activate-instance` separate.

The OpenClaw executable must resolve to version `2026.7.1`, with the exact reload
plan, settings and handler sources pinned in `restart_policy.py`. A different
version or source retains the legacy decision. No executable, health, model or
authentication probe is run by this policy check.

## Supported decision

Compare parsed configurations at command entry and successful exit. Only the
known fields written by these commands are recognized; unknown changes keep
the external restart fallback. Reload policy transitions, unsupported settings,
custom plugin loading/install records or unknown plugin entries, and debounce delays above 300 ms also
keep the fallback. A pending `client.restart_required` is never cleared.

| Existing reload mode | Recognized configuration changes | No configuration changes |
| --- | --- | --- |
| omitted / hybrid | No extra restart | No extra restart |
| restart | No extra restart | No extra restart |
| hot | Extra restart for auth / Control UI / other recognized restart-only fields | No extra restart |
| off | Extra restart | No extra restart |
| unknown / invalid | Extra restart | Extra restart |

Decisions use the actual fields, not the command name. In particular, the first
configure of a preinstalled agent can change gateway authentication and Control
UI settings, while a subsequent model-key-only configure can be hot-reloaded.
An all-skipped attach batch can still modify tools and require a restart in off
mode; the number of added agents alone does not determine its result.

VPS and ordinary Fly commands retain their existing result and client-flag
aggregation. Fly activation retains its own staged publication, config hash,
`activationRequired` and supervisor acknowledgment contract.

## Delivery timing

The policy classifies the intended configuration update. OpenClaw's file
watcher, debounce, hot reload or internal restart can finish after CLI return.
Consequently Host command completion can now precede fresh WebSocket auth and
new model settings becoming usable. Removing the extra container restart also
removes its associated readiness wait. Delivery callbacks or immediate UI
connections must not interpret `restartRequired=false` as an effective-state
barrier. This remains a rollout decision for the caller; no new caller protocol,
fixed production sleep or inference probe is introduced here.

## Local verification

`tests/host_reload_contract.ps1` accepts a frozen candidate directory, the
existing offline probe fixtures, template ZIPs and a new results directory.
It mounts the candidate at the standard Host path and runs only disposable,
labeled local containers on the fixed Host base image with 2 CPU, 2 GiB and
256 PIDs, with networking disabled. It checks the returned decision, applies
the caller restart only when required, and then verifies new WS authentication,
live agents/models, actual requests to the offline model service, container
identity / start time / PID, nginx syntax and a served public workspace file.
Test-only stabilization deliberately does not prove immediate application at
CLI return. Neither public Edge routing nor real provider inference is covered.
