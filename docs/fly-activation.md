# Fly activation handshake

`activate-instance` is separate from `create-instance`, `configure-instance`,
and `add-agent`. Those existing workflows and runtime detection are unchanged.
The image must already contain configured agents and their workspaces. No zip,
template extraction, agent registration, service restart, or effective-state
acknowledgment is performed by this command.

Both explicit environment guards are required:

```sh
UNITAG_AGENT_MANAGER_RUNTIME=container \
UNITAG_AGENT_MANAGER_ACTIVATION_MODE=fly \
agent-manage activate-instance --model-key-stdin
```

Supply the model key through stdin using the existing secret-input protocol.
Only `--model-key-stdin`, `--model-env`, `--ai-shop`, `--image-quality` and
`--base-url` command arguments are accepted. Container path overrides remain
forbidden. Template, zip, local mode, model override, plaintext model key,
workspace root and no-rollback arguments are not accepted. Legacy configure
flags are unchanged. CG's `ControlOptions.FlyManagedActivation` opt-in supplies
the two environment guards only for this new command.

## Result contract

The normal structured response contains the configure-existing data plus:

```json
{
  "result": {
    "mode": "configured",
    "requestedConfigSha256": "<64 lowercase hex characters>",
    "config_path": "/home/node/.openclaw/openclaw.json",
    "steps": [],
    "total_elapsed_ms": 0
  },
  "activationRequired": true,
  "restartRequired": false
}
```

`requestedConfigSha256` is SHA-256 of the exact raw configuration bytes
published by this transaction, not reserialized JSON or an effective runtime
configuration hash. Root-level `activationRequired=true` requests a parent CG/supervisor
reload/restart acknowledgment. A successful CLI response proves configuration
publication only. The parent owns the pinned OpenClaw callback inspection and
exact-source-hash acknowledgment; AgentManager never claims the config is active.
The existing `restartRequired` field does not replace this new marker.

Dry-run publishes nothing and returns `activationRequired=false`,
`requestedConfigSha256=null`, and `dryRun=true`.

## Configuration transaction

The new path configures a private temporary config in the same root, retaining
the existing root-relative skill installation and workspace configuration.
Only after all configure steps succeed does it atomically replace the final
config once. The `.bak` contains the original raw config. Configuration failure,
workspace failure or publication failure discards staging without restoring
over a concurrent writer; a failed final replace restores the previous backup.
Step timings include snapshot, staging, existing configure steps and commit.

An exclusive, nonblocking descriptor-scoped advisory lock rejects overlapping
new-command transactions (`flock` on POSIX; byte-range locking on Windows).
The private regular lock file must belong to the workload UID on POSIX; links,
unsafe permissions and replaced inodes are rejected. Its inode is retained,
not deleted or truncated. A process exit, kill or executor timeout releases the
kernel lock automatically; an existing unlocked file does not block retries.
A raw-byte/file-identity snapshot check immediately before publication detects
intervening writes by other commands. Non-cooperating external writers are not
locked: the last check and replace are not a filesystem compare-and-swap, so a
writer racing exactly inside that interval cannot be fully excluded. The parent
must serialize other config writers for a strict guarantee. Never delete the
lock file to bypass a busy lock: that would create independently locked inodes.
Forced termination can leave private staging files, but not a held kernel lock.

The lock correction changes the copied AM source and requires a new immutable
candidate build and acceptance. Image `sha256:4e42920156557193682035178d8b02623b743f9f87c75e40d7661beac6a1ac84`
contains the superseded file-existence lock and is not final acceptance of this fix.

This is a configuration-file transaction, not a multi-file workspace transaction.
Skills/policy writes completed before a failure can remain; the final config is
not published and no activation marker is returned. No reload is triggered here.
