# Shared Host OpenClaw Chat registration (unreleased)

Base: `1.0.002`, commit `24adaaea256bee8e6d26e9e14c9381532e2a2926`.
This is an explicit, static Host registration mode. The original local-runtime
mode, CLI, private socket and internal ticket protocol remain unchanged.

## Ownership and transport

DockerManager owns lifecycle, the current container generation, installation
associations, endpoints and mount layout. It writes this trusted configuration
outside tenant-writable volumes. Manager has read-only mounts of each runtime's
configuration and history, its own private writable state directory, and no
Docker socket. The Host Server API and Manager share the private Unix socket.
Do not mount Manager state or its socket in an OpenClaw container.

Start with `python -m agent_manage.server --config /private/server.json`.
Install the existing `server` extra. The registered `executable` points to an
accessible OpenClaw package used for version discovery; it is not a docker-exec
wrapper and is never invoked for Host-bound Chat management writes. Exact native
version confirmation is still required at handshake.

```json
{
  "stateDirectory": "/private/manager",
  "runtimes": [{
    "id": "instance-A",
    "engine": "openclaw",
    "environmentId": "instance-A",
    "home": "/mounts/instance-A",
    "executable": "/opt/openclaw/openclaw.mjs",
    "configPath": "/mounts/instance-A/config/openclaw.json",
    "lifecycle": "external",
    "gatewayUrl": "ws://127.0.0.1:28001",
    "gatewayOrigin": "http://127.0.0.1:28001",
    "hostBinding": {
      "bindingId": "container-generation-A1",
      "nativeConfigPath": "/home/node/.openclaw/openclaw.json",
      "pathMappings": [
        {"native": "/home/node/.openclaw", "local": "/mounts/instance-A/config"},
        {"native": "/home/node/.openclaw/workspace", "local": "/mounts/instance-A/workspace"}
      ],
      "agentBindings": [
        {"agentIdentify": "order-agent-A", "nativeAgentId": "main",
         "installationId": "installation-A1", "workspace": "/home/node/.openclaw/workspace"},
        {"agentIdentify": "order-agent-B", "nativeAgentId": "attached-agent",
         "installationId": "installation-B1", "workspace": "/home/node/.openclaw/workspace/attached-agent"}
      ]
    }
  }]
}
```

Add another runtime with its own ID, home, local mount roots, endpoint and native
Gateway token. The native paths may be identical. Longest native prefix wins so
a nested workspace mount does not accidentally resolve under the config mount.
Absolute native paths must be POSIX, traversal-free and mapped. Local paths must
be absolute and link-free. Local mount roots cannot overlap across bound runtimes.
Manager must see these exact local mount paths in its own namespace: a Host path
not mounted inside Manager is not readable merely because DockerManager knows it.
Manager private state must not overlap any registered tenant mount. Shared-Host
configuration reads are bounded at 8 MiB and session-index reads at 64 MiB;
history transcript budgets remain those of the v1 API. Strict POSIX directory
opens reject links in every path component, including an indexed root alias.

## Identity and native connection

Each bound runtime requires its own nonempty, plaintext token in the mounted
native `gateway.auth.token`. Tokens are not returned through this API or logged.
Env-only credentials, SecretRefs and trusted-proxy authentication are not
implemented by this adapter. This does not introduce a credentialFile.

For a Host-bound Chat send, Manager subscribes to that exact native session
before dispatching `chat.send` on the same Gateway connection. Some OpenClaw
runs deliver deltas and terminal events only to session subscribers, not to
the generic Control UI broadcast. The subscription outlives browser reconnects
and is released when the run terminates. Native connection loss is still
reported as an unknown observation; Manager does not resend a write.

Plain `ws` is accepted only for loopback. Remote endpoints require `wss` with
normal TLS validation. Use a safely exposed Host-loopback container port, or a
trusted TLS endpoint; do not relax this to arbitrary container-IP plaintext.

Manager performs the normal signed-device challenge and native authentication.
The key is scoped to runtime ID, container `bindingId`, endpoint, Origin, local
and native config paths and mount mappings. A changed binding uses a new key and
requires normal OpenClaw pairing/authorization. Device validation is never
disabled or silently bypassed.

Before a connection is considered ready, Manager checks native protocol 4, the
exact installed package version, `config.get.path` against `nativeConfigPath`,
and `config.get.hash` against SHA256 of the original local config bytes. Missing
hashes, path/version/hash mismatches, or a config changed during handshake fail
closed. The registration token fingerprint is pinned until Manager is restarted
with a reviewed registration. A wrong endpoint is not repaired by trying other
runtimes. This is a trusted-registration consistency proof, not attestation of a
hostile Gateway or its container runtime.

## Resolve a business installation before issuing an internal ticket

Only the private Unix socket accepts this route. It is not a browser method,
does not register or mutate runtimes, does not redeem platform tickets and does
not callback to VPSManagement. TCP and requests containing Origin are rejected
by the same boundary as all Manager routes.

```http
POST /v1/agents/resolve
Content-Type: application/json

{"runtimeId":"instance-A","agentIdentify":"order-agent-B"}
```

```json
{"ok":true,"result":{
  "runtimeId":"instance-A","agentIdentify":"order-agent-B",
  "agentId":"agent_<opaque-stable-id>",
  "bindingId":"container-generation-A1","installationId":"installation-B1"
}}
```

Host compares runtime, business Agent, bindingId and installationId to its
current trusted placement before signing an internal scoped ticket. Browser
input cannot select a different registration, native Agent, path or endpoint.
Normal internal `/v1/auth/tickets` and `/v1/ws` first-frame auth remain unchanged.
No per-container audience is required; the existing internal Manager authority
uses its normal service-wide audience, not an external platform contract.

There is no title/default/first-agent fallback. Every bound Agent must exist
exactly once in `agents.list` and its configured/default workspace must exactly
match the registration. Additional native Agents without business bindings are
not exposed. An entirely incorrect but self-consistent trusted Host registration
cannot be disproved by Manager; the installation record remains Host authority.

ID rules:

- `runtimeId` is the stable business instance ID, not container IP/ID.
- `bindingId` changes with a container generation or rebinding and isolates the
  device key. Update the static registration and restart Manager; live reload
  and automatic discovery are not implemented.
- `installationId` identifies the installed business association. Preserve it
  across a container rebuild restoring the same installation. Change it for a
  genuinely new installation, even if it reuses `main` and the same workspace.
- Manager Agent IDs include runtime, native Agent ID, business Agent identify
  and installationId. Thread IDs also include the installed Manager Agent ID.
  Rebuild preserves IDs; a new installation produces new IDs. Old Agent/thread
  scopes and cursor references cannot authorize the replacement installation.
- Host must clean or deliberately migrate native history when replacing an
  installation. Manager identity versioning is not a native data-erasure policy.

## Supported scope and acceptance

Host-bound adapters expose text Chat, thread operations, local history and runs.
Local management/installation/model/channel/service writes, recovery and media
access explicitly return `UNSUPPORTED`, including under wildcard leases.
Text Chat itself may use tools inside the native container; these API limits do
not sandbox tool code. Original local-runtime media/management behavior remains.

Local automated tests cover two same-native-path registrations, distinct mapped
history markers, nested workspace mapping, missing/wrong/duplicate bindings,
symlink rejection, stale installation scopes, stable rebuild IDs, device key
isolation, private Unix-socket resolution, scoped leases, and native handshake
signature/token/path/version/hash checks against a protocol fixture. A fixture
is not a real OpenClaw container acceptance.

Still required: one shared Manager and two real independent July containers,
normal device pairing, native raw config hash confirmation, live marker replies
from both models, attached-Agent selection, wrong endpoint/config rejection,
real rebuild and reconnect, Host generation comparison, and end-to-end platform
ticket enforcement. No paid model call, deployment or publication is implied.

The local source freeze is recorded in
[`v1-shared-host-source-manifest.json`](v1-shared-host-source-manifest.json).
