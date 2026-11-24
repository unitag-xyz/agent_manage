# Chat2 Host WebSocket Handoff

Use the existing authenticated `POST /api/frontend/agent/{agentIdentify}/chat/ticket`
and open the returned `wsUrl`. The first WebSocket frame is
`{"type":"auth","ticket":"<one-time-platform-ticket>"}`. Subsequent requests use
the `runtimeId` from `hello`; do not attach the ticket to every request.
Reconnect with a new ticket and do not resend writes automatically.

## Attachments

There is no HTTP upload, staging, chunk/commit call, reference ID, or independent
upload expiry. Attach files inline on the existing WebSocket:

```json
{"type":"request","requestId":"transport-1","method":"runs.create","params":{
  "runtimeId":"instance","threadId":"thread_...","expectedCurrentSegmentId":"segment_...",
  "message":"Describe this screenshot","clientRequestId":"stable-send-id",
  "attachments":[{"type":"image","mimeType":"image/png","fileName":"screenshot.png","content":"<plain base64>"}]
}}
```

Limits: five items; 23 MiB of combined base64 text (17.25 MiB of original
content), 24 MiB including JSON, 6 MiB per image. Supported declared types
are PNG, JPEG, WebP, GIF, PDF and plain text. No data-URL prefix, local
paths, remote URLs or video uploads. Invalid types and base64 are rejected
before dispatch; the native model may impose a lower limit.

The local discovery addition is `capabilities.get {runtimeId}`. An authorized
instance grant returns the normal method catalog and an optional
`attachments:{mode:"inline",maxCount,maxBase64Bytes,maxImageBytes,maxRequestBytes,mimeTypes}`.
If an older deployment rejects discovery or omits the field, do not infer that
the large limit is enabled. Use deployment configuration or keep conservative
legacy limits. The gateway can be configured with a lower request limit.

Keep `clientRequestId` stable for the same exact payload. The initial success
is an Operation acceptance, not proof that the message reached OpenClaw.
If the receipt was lost, re-sign the connection, then query
`runs.lookup {runtimeId,threadId,clientRequestId}`. Do not automatically replay
unknown outcomes. Cancel an accepted Run using its opaque `runId`, not an
attachment ID. Cancelling an unsent draft is local only.

## Recovery, history and model controls

- Sidebar/list/detail expose `runActivity:{source,activeRuns,activeRunId,lastRun}`.
  `activeRunId` is present only for exactly one Manager-observed run. If there
  are multiple, show the available opaque Run IDs and let the user pick one.
  Use `runs.get {runtimeId,runId}` to check the exact Run and
  `runs.cancel {runtimeId,runId,clientRequestId}` to stop it. The Manager checks
  the Run's thread and runtime scope. The local ownership guard also requires
  its originating Operation to belong to the requesting operator; it is not
  yet deployed. An empty list does not rule out external
  native activity; `observation:unknown` does not establish completion.
- A terminal observation may include `completedAt` and
  `resultBoundary:{segmentId,status,candidateAssistantMessageId?}`. A candidate
  is not a persisted receipt. Match an exact transcript identifier and
  segment; never deduplicate a turn by text or timestamp. Unread markers
  remain client state; the backend does not maintain unread counts.
- Use `segments.list` and `messages.list`; use that segment's older/newer
  cursor without substituting a cursor from another segment. At a boundary,
  switch segment and start a fresh page. For previews, use the sidebar title
  or a small, authorized history page. Browser navigation keeps opaque
  thread IDs; instance and thread authorization still apply on reopening.
- Read model choices and effective model using `threads.models.list`. If the
  pinned model is gone, send `threads.models.update {runtimeId,threadId,modelId:null,clientRequestId}`,
  wait for the Operation and reread before sending. Do not send after
  `THREAD_BUSY` or an unknown update outcome.
- For a lost create receipt, use
  `operations.lookup {runtimeId,operationMethod:"threads.create",clientRequestId}`.
  Do not pass a thread ID before creation. After completion, use
  `items[0].result.threadId`. Rename uses the same lookup with
  `operationMethod:"threads.update"` and the known `threadId`.

## Media and generation failures

`messages.list.result.items[]` and `messages.get.result.message` always include
`statistics` on the new history adapter. A null value means this message has
no recorded statistics; an omitted key means an older deployment. The field
is independent of text/thinking projection and full-message references and
works on current and archived segments. Keep separate messages keyed by
segment and message ID.

```json
{"provider":"provider","model":"model","responseId":"resp_1","durationMs":250,"usage":{"input":10,"output":3,"cacheRead":0,"cacheWrite":0,"cost":{"total":0}}}
```

IDs, labels, duration, cost and counters are optional when not recorded.
Zero is a recorded value. Input already excludes cached tokens. Tool result
statistics use `{toolCallId,mediaGeneration:{kind,outputCount,provider?,model?,durationSeconds?,taskId?}}`.
These fields do not contain tool arguments, internal paths or resource URLs.
Do not use `sessions.usage.logs` to backfill a present null. The old bounded
logs fallback can still be used on deployments where the key is absent.

`sessions.usage.logs` returns recorded transcript counters keyed by message ID.
For structured native image/video/music or TTS tool results it can also include
`toolCallId` and `mediaGeneration:{kind,outputCount,provider?,model?,durationSeconds?,taskId?}`.
An opaque `runId` is present only when an exact native ID maps to the same
thread and segment. Missing output metrics, provider cost or run
links must remain unknown. Do not infer them from task timestamps.

Read history resources through `resources.get` with the opaque `resourceId`,
`offset` and `length` up to 128 KiB. Check the same SHA-256 across chunks.
There is no public media URL or thumbnail/original relationship guaranteed by
this contract. A local thumbnail may be rendered after authorized image reads.

The pinned OpenClaw emits unstructured failure text; the browser receives
redacted `RUN_FAILED`. It is not safe to use that code to decide that Codex
needs a new login. Existing manual login and logout are unchanged.

## Verification and rollout status

- Production `twhyc628`: an intentionally malformed 400 KiB attachment request
  passed the web bridge and was rejected before dispatch. No production
  message, refresh, cancel or delete was executed.
- Native OpenClaw 2026.7.1 in the local Host image: real WebSocket send of a
  roughly 769 KiB PNG and a 1x1 PNG together reached a loopback model fixture
  as two images, generated a final response and returned the reply in native
  history. This did not exercise browser upload or history media replay.
- Production also returned the thread activity shape, segments, model restore
  capability, a successful no-thread create-operation lookup, and five
  attached Agent refresh targets on a newly issued platform ticket.
- The inline attachment limit is already deployed on Contabo and server-000.
  The new capability discovery, audio output summary and strict cancel
  ownership check are local changes only. No public endpoints or old Chat
  routes were changed.
