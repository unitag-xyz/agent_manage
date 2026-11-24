# Shared Host local validation

Date: 2026-11-22 (Asia/Shanghai).

## Source and boundaries

- Worktree: `Unitag.AgentManager.shared-host`.
- Branch: `codex/v1-shared-host-binding`.
- Base: `1.0.002`, `24adaaea256bee8e6d26e9e14c9381532e2a2926`.
- Status: local uncommitted changes, no release or version change.
- Source manifest: `v1-shared-host-source-manifest.json`.
- Manifest SHA256: `404ae84a38f3c91be7191b42972b17ca3a69adcbd07d75e49c74bea94880ee93`.
- All nine listed source/test hashes were checked against the worktree.
- No Host, VPSManagement, frontend, main-worktree or DSH prototype edits.
- No deployment, remote mutation, paid model call or production credential access.

## Tests executed

The code was mounted read-only in disposable local Linux Docker containers with
external networking disabled. The successful full regression used existing
`unitag-local/dsh-host:0.2.2-rc.1` solely as a Python 3.11 / Node environment,
not as proof of a unified DSH adapter. Server-extra dependencies were provided
through the isolated `unitag-am-shared-host-deps` volume (`aiohttp 3.14.5`,
`cryptography 50.0.2`). No OpenClaw executable environment variable was supplied.

| Gate | Result |
| --- | --- |
| Full `unittest discover -s tests` | 442 total, 416 passed, 26 skipped, 0 failures/errors |
| Final `unittest discover -s tests -p 'test_v1*.py'`, after the final capability support correction | 42 total, 41 passed, 1 skipped, 0 failures/errors |
| `git diff --check` | Passed |
| Frozen source hash comparison | All nine matched |

The initial Python-only test image lacked Node and produced one unrelated
`FileNotFoundError: node`; the complete suite was rerun in the environment above
without suppressing that test. The skipped tests require a pinned native
OpenClaw installation. The v1 skipped test is the real native Gateway handshake,
send, stream, history and abort integration gate.

Ten new tests cover the trusted static registrations, same native path in two
runtimes, separate local history markers, nested workspace mapping, deterministic
Agent/thread IDs, rebuild device isolation, replacement-installation scope
rejection, incorrect Agent/workspace bindings, shared endpoint/token rejection,
tenant/private-state overlap, strict no-link reads including a linked index root,
private Unix-socket resolution, scoped leases and unsupported Host management
and media. The handshake fixture verifies the actual Ed25519 signature and
token, native path, hash and version checks. It is explicitly a fixture, not a
real OpenClaw device-pairing or container test.

## Remaining acceptance

The Host owner still needs to validate one Manager and two real independent
OpenClaw July containers, normal device pairing, actual `config.get.hash`
semantics, distinct real model replies, attached-Agent resolution, incorrect
endpoint/config binding rejection, real container rebuild/reconnect and platform
ticket enforcement. The resolver checks trusted installed registration; resolving
an ID does not by itself prove native Gateway readiness or model availability.

Do not mark these native/container/business gates complete from the fixture
results. Registration and adapter limits are in `v1-shared-host.md`.
