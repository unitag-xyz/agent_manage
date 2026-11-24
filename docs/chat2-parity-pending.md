# Chat2 Parity: Remaining Integration Design

## Large attachments

The tested OpenClaw build limits a native WebSocket message to 25 MiB.
A single 20 MiB attachment needs about 26.7 MiB in base64, before the RPC
envelope. Browser-side chunking does not remove that upstream limit.
The current native `chat.send` normalizer accepts base64 content, not a
reference to a staged file. Keep the small inline path unchanged until
the receiving runtime supports a staged attachment reference.

The current release uses inline sends only: up to five files, 23 MiB of combined
base64 and a 24 MiB Chat2 message. The Gateway and Manager use independent
admission limits. A 20 MiB raw file is not supported in this mode.

If full legacy attachment capacity is needed later, a potential separate project
streams authenticated chunks into runtime-local staging.
At creation, bind the upload to the authenticated owner, runtime, current
container generation and opaque thread. Cap at five files per turn,
20 MiB per file, with an aggregate per-runtime quota and a short expiry.
Make offset retries idempotent, reject divergent overlap, and verify size
and SHA-256 before the attachment can be referenced. The browser receives
only an opaque ID, never a path or public URL.

At send, consume IDs under the same authorization and the existing
`clientRequestId` Operation. Reject cross-thread and stale-generation
references before dispatch. OpenClaw must consume the staged content inside
the target instance without rebuilding a base64 WebSocket frame. Validate
cleanup, retries, interrupted uploads and revoked sessions before rollout.
No staged-file reader or alternate public route is enabled.
The OpenResponses endpoint is not an equivalent replacement: it assigns a new
response ID internally and can abort its agent command when the HTTP client
disconnects. It does not preserve the current Chat2 deduplication and reconnect
contract.

## Attached Agent management

VPSManagement includes bounded hints for installed, platform-provisioned
attached Agents on the private instance grant. Manager intersects them with
the current container's Agent IDs at sidebar read time. Unmatched or
ambiguous groups do not get a refresh target. The browser passes the returned
`platformAgentIdentify` to the existing authorized refresh operation, which
must still authorize the requester and validate the installation. This is not
a new Chat action; ordinary Chat grants cannot invoke native management.

## Usage and message completion

Use exact IDs recorded in the authorized transcript or in a trusted operation
receipt. A terminal stream event may identify a candidate message and the
expected segment but cannot establish persistence; keep the result unverified
until the matching transcript record is found. Provider-specific and media
usage must not be inferred from timestamps, provider names, or token totals.
When the transcript contains the built-in image or video generation tool's
structured result, expose its output count, provider, model, optional duration
and task ID, linked by the exact tool call. No cost is inferred from this data.
The pinned native chat error event carries unstructured `errorMessage`;
AgentManager emits generic `RUN_FAILED` without forwarding that diagnostic.
Codex-specific automatic recovery requires a typed upstream failure reason.
