"""Short CLI requests plus a detached worker for native device-code OAuth."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .refresh_management import _atomic_bytes, _private_json

GLOBAL_PROFILE = "openai:agent-manage-global"


class CodexAuthMixin:
    def _codex_auth_path(self, name):
        return self.config_path.parent / "agent-manage" / ("codex-" + name + ".json")

    def _codex_auth_read(self, name):
        path = self._codex_auth_path(name)
        if path.is_symlink():
            raise ValueError("Codex state must not be a link")
        value = json.loads(path.read_text()) if path.exists() else {}
        if value and value.get("config_path") != str(self.config_path):
            raise ValueError("Codex state belongs to another environment")
        return value

    def _codex_auth_agents(self, config):
        root = self.config_path.parent
        agents = []
        for agent in config.get("agents", {}).get("list", []):
            directory = Path(agent.get("agentDir") or root / "agents" / agent["id"] / "agent").expanduser().resolve()
            agents.append({"id": agent["id"], "dir": str(directory)})
        # Include persisted stores for removed agents too: global logout must not leave their OAuth behind.
        for directory in sorted((root / "agents").glob("*/agent")):
            resolved = str(directory.resolve())
            if not any(agent["dir"] == resolved for agent in agents):
                agents.append({"id": directory.parent.name, "dir": resolved})
        return agents

    def _codex_bridge_input(self, action, **extra):
        binary = shutil.which(self.bin, path=self.runner._command_env().get("PATH"))
        if not binary:
            raise FileNotFoundError("OpenClaw executable was not found")
        package = Path(binary).resolve().parent
        metadata = package / "package.json"
        if not metadata.is_file():
            raise ValueError("Cannot locate the server OpenClaw package from its executable")
        info = json.loads(metadata.read_text())
        version = info.get("version")
        if info.get("name") != "openclaw" or not isinstance(version, str) or not re.fullmatch(r"2026\.7\.[1-9][0-9]*(?:-[1-9][0-9]*)?", version):
            raise ValueError(
                "Codex global authentication currently supports server OpenClaw "
                "2026.7.* stable releases only (including numeric packaging revisions); "
                f"detected name={info.get('name')!r}, version={info.get('version')!r}, package={package}"
            )
        for relative in ("dist/plugin-sdk/provider-auth.js", "dist/extensions/openai/openai-chatgpt-device-code.js"):
            if not (package / relative).is_file():
                raise ValueError(f"Server OpenClaw {version} is missing the required Codex SDK module: {relative}")
        root = self.config_path.parent
        config = self._load_config()
        agents = self._codex_auth_agents(config)
        if action == "logout":
            restore = self._codex_auth_read("restore")
            extra["restore_orders"] = restore.get("native_orders", [])
            for directory in restore.get("agent_dirs", []):
                if not any(agent["dir"] == directory for agent in agents):
                    agents.append({"id": Path(directory).parent.name, "dir": directory})
        return {"action": action, "package": str(package), "main": str(root / "agents/main/agent"),
                "agents": agents, "order": config.get("auth", {}).get("order", {}).get("openai"),
                "backup_path": str(self._codex_auth_path("transaction")), **extra}

    def _codex_bridge_process(self, payload):
        node = shutil.which("node")
        if not node:
            raise FileNotFoundError("Node.js is required by the server OpenClaw authentication SDK")
        env = self.runner._command_env()
        env.update(OPENCLAW_STATE_DIR=str(self.config_path.parent), OPENCLAW_CONFIG_PATH=str(self.config_path))
        # Do not inherit overrides that could redirect the native canonical auth store.
        for key in ("OPENCLAW_AGENT_DIR", "PI_CODING_AGENT_DIR", "OPENCLAW_AUTH_STORE_READONLY"):
            env.pop(key, None)
        process = subprocess.Popen([node, str(Path(__file__).with_name("codex_auth_bridge.mjs"))],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, env=env)
        try:
            process.stdin.write(json.dumps(payload))
            process.stdin.close()
        except Exception:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()
            raise RuntimeError("Could not initialize native Codex authentication") from None
        return process

    def _codex_bridge(self, action, **extra):
        process = self._codex_bridge_process(self._codex_bridge_input(action, **extra))
        try:
            # Never route bridge stdout through CommandError: it may contain OAuth credentials.
            process.stdin = None
            output, _ = process.communicate(timeout=30)
            if process.returncode:
                raise RuntimeError("Codex native authentication operation failed")
            return json.loads(output)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise

    def _codex_public_job(self, job):
        if not job:
            return None
        result = {key: job[key] for key in ("status", "verification_url", "user_code", "expires_at", "error_code") if key in job}
        if job.get("status") in {"starting", "pending"} and job.get("expires_at", 0) <= int(time.time() * 1000):
            result = {"status": "expired", "error_code": "CODEX_LOGIN_EXPIRED"}
        return result

    def codex_status(self):
        auth = self._codex_bridge("inspect")
        job = self._codex_public_job(self._codex_auth_read("job"))
        pending_status = job["status"] if job and job["status"] in {"starting", "pending", "failed", "expired"} else "logged_out"
        return {"ok": True, "scope": "environment", "mode": "oauth_global", **auth,
                "status": "logged_in" if auth["logged_in"] else pending_status,
                "login": job, "models_switched": self._codex_models_active(), "activation_verified": False,
                "restart_required": False, "gateway_restarted": False}

    def _codex_launch_worker(self, job):
        env = self.runner._command_env()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        process = subprocess.Popen([sys.executable, "-m", "agent_manage.codex_auth", str(self.config_path), self.bin, job["id"]],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, env=env, close_fds=True)
        # Reap workers when embedded in a long-lived API process; CLI exit still detaches them.
        threading.Thread(target=process.wait, daemon=True).start()

    def codex_login(self):
        if self.runner.dry_run:
            return {**self._codex_configure_models(), "mode": "oauth_global", "authentication_started": False}
        with self._refresh_lock(self.config_path.parent / "agent-manage"):
            if self._codex_auth_path("transaction").exists():
                raise FileExistsError("Codex authentication update was interrupted; run codex-logout before retrying")
            job = self._codex_auth_read("job")
            if self._codex_public_job(job) and self._codex_public_job(job)["status"] in {"starting", "pending"}:
                return {"ok": True, "scope": "environment", "mode": "oauth_global", "logged_in": False,
                        "restart_required": False, **self._codex_public_job(job)}
            auth = self._codex_bridge("inspect")
            if auth["logged_in"] and self._codex_read_state().get("status") == "active":
                result = self._codex_configure_models_locked()
                return {**result, "mode": "oauth_global", "status": "logged_in", "logged_in": True,
                        "credentials_shared": True, "account": auth["account"]}
            if auth["canonical_auth_available"]:
                return self._codex_finish_login_locked()
            # Resolve/validate the server SDK before starting an asynchronous task.
            self._codex_bridge_input("device-login")
            job = {"config_path": str(self.config_path), "id": uuid.uuid4().hex, "status": "starting",
                   "expires_at": int(time.time() * 1000) + 30000}
            _private_json(self._codex_auth_path("job"), job)
            try:
                self._codex_launch_worker(job)
            except Exception:
                job.update(status="failed", error_code="CODEX_WORKER_START_FAILED")
                _private_json(self._codex_auth_path("job"), job)
                raise RuntimeError("Could not start Codex login worker") from None
        # Bound the command's wait; slower device-code requests are exposed by codex-status.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = self._codex_auth_read("job")
            if current.get("id") != job["id"] or current.get("status") != "starting":
                break
            time.sleep(0.05)
        public = self._codex_public_job(current)
        if public["status"] == "failed":
            raise RuntimeError("Codex device authorization failed; query codex-status or retry codex-login")
        if public["status"] == "completed":
            return self.codex_status()
        return {"ok": True, "scope": "environment", "mode": "oauth_global", "logged_in": False,
                "restart_required": False, **public}

    def _codex_finish_login_locked(self, credential=None):
        original = self.config_path.read_bytes()
        original_mode = stat.S_IMODE(self.config_path.stat().st_mode)
        model_path = self._codex_state_path()
        original_model_state = model_path.read_bytes() if model_path.exists() else None
        auth_path = self._codex_auth_path("restore")
        original_auth_state = auth_path.read_bytes() if auth_path.exists() else None
        config = self._load_config()
        owned_config = original
        restore = self._codex_auth_read("restore") or {
            "config_path": str(self.config_path),
            "order": {provider: {"present": provider in config.get("auth", {}).get("order", {}),
                                 "value": config.get("auth", {}).get("order", {}).get(provider)}
                      for provider in ("openai", "openai-codex")},
            "profile": config.get("auth", {}).get("profiles", {}).get(GLOBAL_PROFILE),
            "auth_present": "auth" in config, "order_present": "order" in config.get("auth", {}),
            "profiles_present": "profiles" in config.get("auth", {}),
        }
        try:
            if self.config_path.read_bytes() != original:
                raise RuntimeError("Config changed before Codex login")
            result = self._codex_configure_models_locked()
            owned_config = self.config_path.read_bytes()
            candidate = self._load_config()
            auth = candidate.setdefault("auth", {})
            auth.setdefault("profiles", {})[GLOBAL_PROFILE] = {"provider": "openai", "mode": "oauth"}
            order = auth.setdefault("order", {})
            order["openai"] = [GLOBAL_PROFILE]
            order.pop("openai-codex", None)
            self._codex_validate(candidate)
            # Keep custom store paths so logout also clears agents subsequently removed from the roster.
            paths = [agent["dir"] for agent in self._codex_auth_agents(candidate)]
            restore["agent_dirs"] = sorted(set(restore.get("agent_dirs", []) + paths))
            # Record auth-order restoration before the first native credential mutation.
            _private_json(auth_path, restore)
            shared = self._codex_bridge("apply", credential=credential)
            orders = restore.setdefault("native_orders", [])
            orders.extend(saved for saved in shared.pop("auth_order_restore", [])
                          if not any(previous["dir"] == saved["dir"] for previous in orders))
            _private_json(auth_path, restore)
            if self.config_path.read_bytes() != owned_config:
                raise RuntimeError("Config changed during Codex login")
            _private_json(self.config_path, candidate)
            owned_config = self.config_path.read_bytes()
            self._codex_auth_path("transaction").unlink()
            return {**result, **shared, "status": "logged_in", "mode": "oauth_global", "logged_in": True}
        except Exception:
            try:
                if self._codex_auth_path("transaction").exists():
                    self._codex_bridge("rollback")
                    self._codex_auth_path("transaction").unlink()
                if self.config_path.read_bytes() != owned_config:
                    raise RuntimeError("Concurrent config changes require explicit recovery")
                _atomic_bytes(self.config_path, original, original_mode)
                for path, data in ((model_path, original_model_state), (auth_path, original_auth_state)):
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_bytes(path, data)
            except Exception:
                raise RuntimeError("Codex login rollback is incomplete; run codex-logout to recover") from None
            raise RuntimeError("Codex global login could not be applied; previous configuration was restored") from None

    def codex_logout(self):
        if self.runner.dry_run:
            return {"ok": True, "scope": "environment", "mode": "oauth_global", "status": "preview", "skipped": True}
        with self._refresh_lock(self.config_path.parent / "agent-manage"):
            # Generation fence: the worker must re-read this under the same lock before installing credentials.
            job = self._codex_auth_read("job")
            if job:
                job.update(status="cancelled")
                for key in ("user_code", "verification_url"):
                    job.pop(key, None)
                _private_json(self._codex_auth_path("job"), job)
            result = self._codex_restore_models_locked()
            auth = self._codex_bridge("logout")
            restore = self._codex_auth_read("restore")
            original = self.config_path.read_bytes()
            config = self._load_config()
            settings = config.get("auth", {})
            if restore:
                profiles = settings.get("profiles", {})
                if restore["profile"] is None:
                    profiles.pop(GLOBAL_PROFILE, None)
                else:
                    profiles[GLOBAL_PROFILE] = restore["profile"]
                order = settings.get("order", {})
                for provider, saved in restore["order"].items():
                    if order.get(provider) == [GLOBAL_PROFILE] or provider not in order:
                        if saved["present"]:
                            order[provider] = saved["value"]
                        else:
                            order.pop(provider, None)
                    elif GLOBAL_PROFILE in order.get(provider, []):
                        order[provider] = [value for value in order[provider] if value != GLOBAL_PROFILE]
                for key in ("order", "profiles"):
                    if not settings.get(key) and not restore[key + "_present"]:
                        settings.pop(key, None)
                if not settings and not restore["auth_present"]:
                    config.pop("auth", None)
                self._codex_validate(config)
                if self.config_path.read_bytes() != original:
                    raise RuntimeError("Config changed during Codex auth restore; retry codex-logout")
                _private_json(self.config_path, config)
                self._codex_auth_path("restore").unlink()
            self._codex_auth_path("transaction").unlink(missing_ok=True)
            return {**result, **auth, "mode": "oauth_global", "status": "logged_out", "logged_in": False,
                    "pending_login_cancelled": bool(job), "server_session_revoked": False}


def run_login_worker(config_path, binary, job_id):
    from .local import LocalRunner
    from .orchestrator import InstanceManagerV2
    manager = InstanceManagerV2(LocalRunner(openclaw_bin=binary), config_path=config_path)
    process = None
    try:
        payload = manager._codex_bridge_input("device-login", job_path=str(manager._codex_auth_path("job")), job_id=job_id)
        process = manager._codex_bridge_process(payload)
        for line in process.stdout:
            event = json.loads(line)
            # Refresh can hold the lock briefly; wait without letting stale auth apply after logout.
            deadline = time.monotonic() + manager.OPENCLAW_COMMAND_TIMEOUT_SECONDS
            while True:
                try:
                    with manager._refresh_lock(manager.config_path.parent / "agent-manage"):
                        job = manager._codex_auth_read("job")
                        if job.get("id") != job_id or job.get("status") not in {"starting", "pending"}:
                            return
                        if event["event"] == "verification":
                            job.update({key: event[key] for key in ("verification_url", "user_code", "expires_at")})
                            job["status"] = "pending"
                        elif event["event"] == "credential":
                            manager._codex_finish_login_locked(event["credential"])
                            job = {"id": job_id, "config_path": str(manager.config_path), "status": "completed"}
                        else:
                            raise RuntimeError("device_auth_failed")
                        _private_json(manager._codex_auth_path("job"), job)
                    break
                except FileExistsError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("auth_lock_timeout") from None
                    time.sleep(0.2)
        if process.wait() != 0:
            raise RuntimeError("device_auth_failed")
    except Exception:
        # Never publish exception text: it may originate from a token response or native SDK.
        try:
            with manager._refresh_lock(manager.config_path.parent / "agent-manage"):
                job = manager._codex_auth_read("job")
                if job.get("id") == job_id and job.get("status") in {"starting", "pending"}:
                    job = {"id": job_id, "config_path": str(manager.config_path), "status": "failed", "error_code": "CODEX_AUTH_FAILED"}
                    _private_json(manager._codex_auth_path("job"), job)
        except Exception:
            pass
    finally:
        if process:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


if __name__ == "__main__":
    run_login_worker(*sys.argv[1:])
