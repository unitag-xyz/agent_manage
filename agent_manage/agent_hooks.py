"""Explicit agent lifecycle hooks, separate from template/config rollback."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
from pathlib import Path

from .file_io import write_private_json
from .local import CommandError, CommandResult
from .template_safety import validate_safe_name

INSTALL_TIMEOUT = 1800
START_TIMEOUT = 120
OUTPUT_LIMIT = 16 * 1024


class HookError(CommandError):
    def __init__(self, agent: str, hook: str, workspace: Path, result: CommandResult):
        self.error_code = f"AGENT_{hook.upper()}_FAILED"
        self.hook_details = {"agent_name": agent, "hook": f"{hook}.sh", "workspace": str(workspace),
                             "workspace_preserved": True, "rollback_performed": False}
        super().__init__(f"Agent '{agent}' {hook}.sh failed (exit {result.returncode})", result)


def _secrets(config, env):
    values = []
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if re.search(r"key|token|password|secret", key, re.I) and isinstance(item, str) and item:
                    values.append(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(config)
    visit(env)
    return values


def _redact(text, secrets):
    for value in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return re.sub(r'''(?im)((?:api[_-]?key|password|token|secret)["']?\s*[:=]\s*)([^\s,]+)''',
                  r"\1[REDACTED]", text)


def _execute(script, workspace, env, timeout, secrets, cancel=None, identity=None):
    """Bounded output and process-group timeout; detached children cannot hold stdout open."""
    retain_limit = OUTPUT_LIMIT + max((len(s.encode("utf-8")) for s in secrets), default=0)
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    with selectors.DefaultSelector() as selector:
        process = subprocess.Popen(["bash", str(script)], cwd=workspace, env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, **(identity or {}))
        for name in tails:
            pipe = getattr(process, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            while True:
                stopped = cancel is not None and cancel.is_set()
                if stopped or time.monotonic() >= deadline:
                    timed_out = not stopped
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    # Kill remaining non-detached descendants too, even if bash exited.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                exited = process.poll() is not None
                for key, _ in selector.select(0 if exited else 0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    totals[name] += len(chunk)
                    tails[name].extend(chunk)
                    del tails[name][:-retain_limit]
                if exited:
                    # Drain available data, never wait for a background daemon's EOF.
                    for key in list(selector.get_map().values()):
                        for _ in range(16):
                            try:
                                chunk = os.read(key.fileobj.fileno(), 65536)
                            except BlockingIOError:
                                break
                            if not chunk:
                                break
                            totals[key.data] += len(chunk)
                            tails[key.data].extend(chunk)
                            del tails[key.data][:-retain_limit]
                    break
        finally:
            process.stdout.close()
            process.stderr.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    output = {}
    for name, tail in tails.items():
        output[name] = ("[output truncated; last 16 KiB]\n" if totals[name] > OUTPUT_LIMIT else "") + _redact(tail.decode("utf-8", "replace"), secrets)[-OUTPUT_LIMIT:]
    if timed_out:
        output["stderr"] += f"\nHook timed out after {timeout} seconds"
    code = 124 if timed_out else 130 if cancel is not None and cancel.is_set() else process.returncode
    return CommandResult(["bash", str(script)], f"bash {script}", code, **output, timed_out=timed_out)


def run_hook(config_path: Path, agent: str, workspace: Path, hook: str, *, dry_run=False,
             force=False, timeout=None, cancel=None):
    validate_safe_name(agent, "agent_name")
    if hook not in ("install", "start"):
        raise ValueError("Unknown hook")
    workspace = workspace.expanduser().resolve()
    script = workspace / f"{hook}.sh"
    info = {"agent_name": agent, "hook": f"{hook}.sh", "workspace": str(workspace)}
    if not script.exists() and not script.is_symlink():
        return {**info, "skipped": True, "reason": "script_missing"}
    if dry_run:
        return {**info, "skipped": True, "reason": "dry_run"}
    result = CommandResult(["bash", str(script)], f"bash {script}", 1, "", "")
    state_path = config_path.parent / "agent-manage" / "hooks" / f"{agent}.json"
    state = {}
    try:
        if script.is_symlink() or not script.is_file():
            raise ValueError("Hook must be a regular file, not a symbolic link")
        owner = config_path.parent.stat()
        for path in (state_path.parent.parent, state_path.parent, state_path, state_path.with_suffix(".lock")):
            if path.is_symlink():
                raise ValueError("Hook state paths must not be symbolic links")
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        def set_owner(path):
            if os.geteuid() == 0:
                os.chown(path, owner.st_uid, owner.st_gid)
        for directory in (state_path.parent.parent, state_path.parent):
            set_owner(directory)
        def save_state():
            write_private_json(state_path, state)
            set_owner(state_path)
        with (state_path.with_suffix(".lock")).open("a") as lock:
            set_owner(state_path.with_suffix(".lock"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another install/start is running for this agent") from None
            if state_path.exists():
                state = json.loads(state_path.read_text())
            same_workspace = state.get("workspace") == str(workspace)
            digest = hashlib.sha256(script.read_bytes()).hexdigest()
            if hook == "install" and not force and same_workspace and state.get("install_status") == "complete" and state.get("install_sha256") == digest:
                return {**info, "skipped": True, "reason": "already_installed"}
            if hook == "start" and ((workspace / "install.sh").exists() or (same_workspace and state.get("install_status"))) and not (same_workspace and state.get("install_status") == "complete"):
                return {**info, "skipped": True, "reason": "installation_incomplete"}
            config = json.loads(config_path.read_text())
            env = dict(os.environ)
            env.update(AGENT_ID=agent, AGENT_WORKSPACE=str(workspace), OPENCLAW_CONFIG_PATH=str(config_path),
                       DEEPTUTOR_OPENCLAW_CONFIG=str(config_path))
            identity = None
            if os.geteuid() == 0 and config_path.parent.stat().st_uid != 0:
                import pwd
                account = pwd.getpwuid(config_path.parent.stat().st_uid)
                identity = {"user": account.pw_uid, "group": account.pw_gid, "extra_groups": []}
                env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
            secrets = _secrets(config, env)
            if hook == "install":
                state.update(workspace=str(workspace), install_status="running")
                save_state()
            result = _execute(script, workspace, env, timeout or (INSTALL_TIMEOUT if hook == "install" else START_TIMEOUT), secrets, cancel, identity)
            state.update(workspace=str(workspace))
            state[f"{hook}_status"] = "complete" if result.returncode == 0 else "failed"
            state[f"{hook}_result"] = {"returncode": result.returncode, "timed_out": result.timed_out,
                                      "stdout": result.stdout, "stderr": result.stderr}
            if hook == "install" and result.returncode == 0:
                state["install_sha256"] = digest
            save_state()
            if result.returncode:
                raise HookError(agent, hook, workspace, result)
            return {**info, "skipped": False, **state[f"{hook}_result"]}
    except HookError:
        raise
    except Exception as exc:
        # Infrastructure errors are also hook failures: never purge a live workspace.
        result.stderr = _redact(f"{type(exc).__name__}: {exc}", _secrets({}, os.environ))
        raise HookError(agent, hook, workspace, result) from exc


def start_agents(config_path: Path, *, dry_run=False, cancel=None):
    config = json.loads(config_path.read_text())
    results = []
    for agent in config.get("agents", {}).get("list", []):
        if cancel is not None and cancel.is_set():
            break
        if not isinstance(agent, dict) or not agent.get("id") or not agent.get("workspace"):
            continue
        try:
            results.append(run_hook(config_path, agent["id"], Path(agent["workspace"]), "start", dry_run=dry_run, cancel=cancel))
        except (HookError, ValueError) as exc:
            item = {"agent_name": agent["id"], "ok": False, "error": str(exc)}
            if isinstance(exc, HookError):
                item.update(returncode=exc.result.returncode, stderr=exc.result.stderr, stdout=exc.result.stdout,
                            timed_out=exc.result.timed_out)
            results.append(item)
    return {"ok": not any(item.get("ok") is False for item in results), "agents": results}
