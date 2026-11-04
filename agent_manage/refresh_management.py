"""Backed-up, conflict-aware local template and model catalog refreshes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .agent_hooks import HookError, run_hook
from .skill_permissions import prepare_shared_skill_path
from .models import RefreshAgentRequest
from .file_io import atomic_write_bytes as _atomic_bytes, write_private_json as _private_json
from .template_safety import validate_safe_name, validate_template_archive


def _fingerprint(path: Path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Refresh requires a regular file: {path}")
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": stat.S_IMODE(path.stat().st_mode)}


def _protected(relative: Path) -> bool:
    # Runtime/user data never becomes template-owned, including on first adoption.
    return any(part.lower() in {"memory", "memory.md", "sessions", "credentials",
                               ".git", ".openclaw", ".env", "auth-profiles.json"}
               or part.lower().startswith(".env.") for part in relative.parts) or (
        relative.name.lower() in {"user.md", "tools.md", "openclaw.json"}
    )


def _safe_target(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError(f"Unsafe refresh path: {relative}")
    target = root / rel
    for ancestor in [target, *target.parents]:
        if ancestor.is_symlink():
            raise ValueError(f"Refresh destination links are not allowed: {ancestor}")
        if ancestor == root:
            break
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Refresh path escapes destination: {relative}")
    return target


class RefreshManagementMixin:
    def refresh_agent(self, request: RefreshAgentRequest) -> dict:
        name = validate_safe_name(request.agent_name, "agent_name")
        if request.models_only and request.template_only:
            raise ValueError("Choose only one refresh scope")
        if request.template_dir and request.agent_zip:
            raise ValueError("Choose only one template source")
        if request.models_only and (request.template_dir or request.agent_zip or request.template_name):
            raise ValueError("Model-only refresh does not accept a template source")
        workspace = self._skill_agent_workspace(name)
        state_root = self.config_path.parent / "agent-manage"
        if self.config_path.is_relative_to(workspace) or state_root.is_relative_to(workspace):
            raise ValueError("Agent workspace must not contain the environment config or refresh state")
        # Resolve sources before mutating anything. Dry-run creates no persistent state.
        with self._refresh_lock(state_root), tempfile.TemporaryDirectory(prefix="agent-manage-refresh-") as temporary:
            if self._skill_agent_workspace(name) != workspace:
                raise RuntimeError("Agent workspace changed while preparing refresh; retry")
            state_path = state_root / "agents" / f"{name}.json"
            if state_path.is_symlink():
                raise ValueError("Installed template state must not be a link")
            previous = json.loads(state_path.read_text()) if state_path.is_file() else {}
            if previous and (previous.get("workspace") != str(workspace)
                             or previous.get("config_path") != str(self.config_path)):
                raise ValueError("Installed template state belongs to another workspace/config")
            original_config = self.config_path.read_bytes()
            config_mode = stat.S_IMODE(self.config_path.stat().st_mode)
            config = self._load_config()
            candidate, models_result = (config, {"skipped": True}) if request.template_only else self._refresh_model_candidate(config)
            plan = []
            conflicts = []
            new_state = None
            template_result = {"skipped": True}
            stage = Path(temporary)
            if not request.models_only:
                source, template_name = self._refresh_source(request, previous, stage)
                new_state, plan, conflicts, template_result = self._refresh_template_plan(
                    name, workspace, source, template_name, previous, stage,
                    request.replace_modified,
                )
            result = {"ok": True, "agent_name": name, "config_path": str(self.config_path),
                      "workspace": str(workspace), "models": models_result,
                      "template": template_result, "conflicts": conflicts,
                      "skipped_conflicts": conflicts if not request.replace_modified else [],
                      "partial": bool(conflicts and not request.replace_modified),
                      "changed_files": [{"path": str(item["target"]), "action": item["action"]} for item in plan],
                      "memory_preserved": True, "skipped": self.runner.dry_run,
                      "gateway_restarted": False, "activation_verified": False,
                      "restart_required": False}
            if self.runner.dry_run:
                result["can_apply"] = True
                return result
            # Validate the candidate without ever replacing the live config first.
            candidate_path = stage / "openclaw.json"
            _private_json(candidate_path, candidate)
            self.runner.run([self.bin, "config", "validate"],
                            timeout=self.OPENCLAW_COMMAND_TIMEOUT_SECONDS,
                            env_overrides={"OPENCLAW_CONFIG_PATH": str(candidate_path),
                                           "OPENCLAW_STATE_DIR": str(self.config_path.parent)})
            if request.restart:
                self._assert_refresh_gateway_scope()
            # Repair only managed shared-file parent directories, including on
            # otherwise no-op refreshes. Permission repair is durable maintenance;
            # content rollback must not make these directories read-only again.
            if new_state is not None:
                shared_root = self.config_path.parent / "skills"
                directories = set()
                for key in new_state["files"]:
                    if not key.startswith("common/"):
                        continue
                    target = _safe_target(shared_root, key.split("/", 1)[1])
                    parent = target.parent
                    while parent.is_relative_to(shared_root):
                        directories.add(parent)
                        parent = parent.parent
                for directory in sorted(directories, key=lambda path: len(path.parts)):
                    prepare_shared_skill_path(directory, self.config_path.parent, recursive=False)
            # Atomic replacement needs directory write/search access, not just
            # a writable destination file. Reject before changing any files.
            for item in plan:
                parent = item["target"].parent
                while not parent.exists():
                    parent = parent.parent
                if not os.access(parent, os.W_OK | os.X_OK):
                    raise PermissionError(f"Refresh directory is not writable: {parent}; "
                                          f"execution uid={os.geteuid()}")
            self._verify_refresh_unchanged(plan, original_config)
            metadata_changed = new_state is not None and any(
                new_state.get(key) != previous.get(key)
                for key in ("template_name", "version", "package_hash", "files", "pending_conflicts")
            )
            if not plan and candidate == config and not metadata_changed:
                if request.restart:
                    self._activate_refresh()
                    result.update(gateway_restarted=True, activation_verified=True, restart_required=False)
                result["backup_path"] = None
                return self._refresh_install(request, workspace, result)
            job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
            backup = state_root / "backups" / job_id
            backup.mkdir(parents=True, mode=0o700)
            backup.chmod(0o700)
            manifest = self._backup_refresh(backup, plan, original_config, config_mode,
                                            state_path, previous, new_state, name, job_id)
            result.update(backup_id=job_id, backup_path=str(backup))
            attempted = []
            config_written = False
            state_written = False
            created_dirs = []
            try:
                self._verify_refresh_unchanged(plan, original_config)
                for item in plan:
                    target = item["target"]
                    _safe_target(item["root"], item["relative"])
                    if _fingerprint(target) != item["before"]:
                        raise RuntimeError(f"File changed during refresh; retry: {target}")
                    attempted.append(item)
                    if item["action"] == "delete":
                        target.unlink()
                    else:
                        missing = []
                        parent = target.parent
                        while not parent.exists():
                            missing.append(parent)
                            parent = parent.parent
                        created_dirs.extend(missing)
                        _atomic_bytes(target, item["source"].read_bytes(), item["after"]["mode"])
                        if item["root"] == self.config_path.parent / "skills":
                            # Atomic writes may create directories as an admin;
                            # keep newly created paths owned by the runtime too.
                            parent = target
                            while parent.is_relative_to(item["root"]):
                                prepare_shared_skill_path(parent, self.config_path.parent, recursive=False)
                                parent = parent.parent
                if candidate != config:
                    if self.config_path.read_bytes() != original_config:
                        raise RuntimeError("Config changed during refresh; retry")
                    config_written = True
                    _private_json(self.config_path, candidate)
                    self.runner.log("config: refreshed model catalog")
                if request.restart:
                    self._activate_refresh()
                    result.update(gateway_restarted=True, activation_verified=True, restart_required=False)
                if metadata_changed:
                    # The baseline is separate from workspace and retained with this backup.
                    baseline = backup / "baseline"
                    baseline.mkdir(mode=0o700)
                    manifest["baseline_files"] = [
                        {"path": key, "file": f"baseline/{index}"}
                        for index, key in enumerate(new_state["files"])
                    ]
                    for index in range(len(new_state["files"])):
                        source_path = stage / "managed" / str(index)
                        shutil.copyfile(source_path, baseline / str(index))
                        (baseline / str(index)).chmod(0o600)
                    new_state["baseline_path"] = str(baseline)
                    new_state["last_refresh_id"] = job_id
                    state_written = True
                    _private_json(state_path, new_state)
                manifest["status"] = "complete"
                _private_json(backup / "manifest.json", manifest)
            except Exception as exc:
                errors = []
                for item in reversed(attempted):
                    try:
                        current = _fingerprint(item["target"])
                        if current == item["before"]:
                            continue
                        if current != item["after"] and current != item["before"]:
                            raise RuntimeError("File changed independently; retain it and report rollback conflict")
                        if item["before"] is None:
                            item["target"].unlink(missing_ok=True)
                        else:
                            _atomic_bytes(item["target"], (backup / item["backup_file"]).read_bytes(), item["before"]["mode"])
                            if item["root"] == self.config_path.parent / "skills":
                                prepare_shared_skill_path(item["target"], self.config_path.parent, recursive=False)
                    except Exception:
                        errors.append(str(item["target"]))
                for directory in created_dirs:
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                if config_written:
                    try:
                        current_config = self.config_path.read_bytes()
                        expected_config = (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode()
                        if current_config not in (expected_config, original_config):
                            raise RuntimeError("Config changed independently; retain it and report rollback conflict")
                        _atomic_bytes(self.config_path, original_config, config_mode)
                    except Exception:
                        errors.append(str(self.config_path))
                if state_written:
                    try:
                        if manifest["state_existed"]:
                            _atomic_bytes(state_path, (backup / "state.json").read_bytes())
                        else:
                            state_path.unlink(missing_ok=True)
                    except Exception:
                        errors.append(str(state_path))
                runtime_restored = None
                if request.restart:
                    runtime_restored = False
                    if not errors:
                        try:
                            self._activate_refresh()
                            runtime_restored = True
                        except Exception:
                            pass
                manifest.update(status="rollback_failed" if errors else "rolled_back",
                                rollback_errors=errors, runtime_restored=runtime_restored)
                _private_json(backup / "manifest.json", manifest)
                raise RuntimeError(json.dumps({"error": f"Refresh failed ({type(exc).__name__})",
                    "rollback": {"ok": not errors, "runtime_restored": runtime_restored},
                    "details": {"backup_path": str(backup), "rollback_ok": not errors,
                                "rollback_errors": errors, "runtime_restored": runtime_restored,
                                "cause": str(exc)}}, ensure_ascii=False)) from exc

            # Installation may change dependencies, services and external files.
            # Run after the refresh transaction, never pretend to roll it back.
            return self._refresh_install(request, workspace, result)

    def _refresh_install(self, request, workspace, result):
        if request.models_only:
            return result
        try:
            result["install"] = run_hook(self.config_path, request.agent_name, workspace,
                                         "install", force=True)
        except HookError as exc:
            exc.hook_details.update(template_refreshed=True,
                                    backup_path=result.get("backup_path"),
                                    partial=result["partial"],
                                    skipped_conflicts=result["skipped_conflicts"])
            raise
        return result

    @contextmanager
    def _refresh_lock(self, root: Path):
        if self.runner.dry_run:
            yield
            return
        for path in [root, root / "agents", root / "backups"]:
            if path.is_symlink():
                raise ValueError(f"Refresh state links are not allowed: {path}")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        lock = root / "refresh.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FileExistsError("Another refresh is running in this environment; retry later") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _refresh_source(self, request, previous, stage):
        name = validate_safe_name(request.template_name or previous.get("template_name") or request.agent_name, "template_name")
        if request.template_dir:
            source = Path(request.template_dir).expanduser()
        else:
            archive_path = Path(request.agent_zip).expanduser() if request.agent_zip else self.template_root / f"{name}.zip"
            if not request.agent_zip and not archive_path.exists():
                source = self.template_root / name
            else:
                if not archive_path.is_file() or archive_path.is_symlink():
                    raise FileNotFoundError(f"Template ZIP not found: {archive_path}")
                if not zipfile.is_zipfile(archive_path):
                    raise ValueError("Refresh template archive must be a ZIP")
                validate_template_archive(archive_path)
                extracted = stage / "extracted"
                extracted.mkdir()
                with zipfile.ZipFile(archive_path) as archive:
                    archive.extractall(extracted)
                    for member in archive.infolist():
                        mode = (member.external_attr >> 16) & 0o777
                        if mode and not member.is_dir():
                            (extracted / member.filename).chmod(mode)
                source = self._resolve_unpacked_root(extracted)
        if source.is_symlink() or not source.is_dir():
            raise FileNotFoundError(f"Template directory not found: {source}")
        return source.resolve(), name

    def _refresh_template_plan(self, name, workspace, source, template_name, previous, stage, force):
        if source.is_relative_to(workspace) or workspace.is_relative_to(source):
            raise ValueError("Template source and workspace must not overlap")
        for item in source.rglob("*"):
            if item.is_symlink() or not (item.is_dir() or item.is_file()):
                raise ValueError(f"Template links and special files are not allowed: {item}")
        manifest = self._load_template_manifest(source)
        version = str(manifest.get("version") or "").strip()
        if not version:
            raise ValueError("Template refresh requires template.yaml version")
        old_version = str(previous.get("version") or "")
        old_match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", old_version)
        new_match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
        if old_match and new_match and tuple(map(int, new_match.groups())) < tuple(map(int, old_match.groups())):
            raise ValueError(f"Refusing template downgrade from {old_version} to {version}; provide the current or newer release")
        if manifest.get("copyMode") == "multi_agent_template" or manifest.get("agents"):
            raise ValueError("Refresh a single agent template; team releases need separate agent packages")
        # Dependency installation cannot be rolled back with a file backup.
        dependencies = []
        for library in self._required_libraries_from_manifest(manifest):
            binary = library.get("bin")
            installed = bool(isinstance(binary, str) and shutil.which(binary))
            dependencies.append({"name": library.get("name") or binary, "installed": installed})
            if not installed and library.get("required") is not False:
                raise ValueError(f"Required dependency missing or not verifiable by bin: {library.get('name') or binary}")
        roots = {"workspace": workspace, "common": self.config_path.parent / "skills"}
        common = self._common_skill_sources_from_manifest(source, manifest)
        common_paths = [item.relative_to(source) for item in common]
        candidates = {}
        preserved = []
        for item in sorted(source.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(source)
            if _protected(relative):
                preserved.append(str(relative))
                continue
            if any(relative.is_relative_to(path) for path in common_paths) or relative.parts[0] == "common-skills":
                continue
            candidates[f"workspace/{relative.as_posix()}"] = item
        # Built-ins take precedence, as in provisioning; merge shared files
        # without removing files that no longer appear in either source.
        builtin_common = self._builtin_common_skill_sources()
        for skill in [*common, *builtin_common]:
            if skill.is_symlink() or not skill.is_dir() or not (skill / "SKILL.md").is_file():
                raise ValueError(f"Common skill must contain SKILL.md: {skill}")
            skill_name = validate_safe_name(skill.name, "skill_name")
            for item in sorted(skill.rglob("*")):
                if item.is_symlink() or not (item.is_dir() or item.is_file()):
                    raise ValueError(f"Skill links and special files are not allowed: {item}")
                relative = Path(skill_name) / item.relative_to(skill)
                if item.is_file() and not _protected(relative):
                    key = f"common/{relative.as_posix()}"
                    if key in candidates and skill not in builtin_common:
                        raise ValueError(f"Duplicate common skill target: {key}")
                    candidates[key] = item
        # Freeze source bytes before applying: callers may replace template caches.
        managed = stage / "managed"
        managed.mkdir()
        files = {}
        for index, (key, item) in enumerate(candidates.items()):
            frozen = managed / str(index)
            shutil.copy2(item, frozen)
            if key.startswith("common/"):
                # Release bundles may be immutable (444/555); runtime copies
                # must remain writable without losing script executable bits.
                frozen.chmod(stat.S_IMODE(frozen.stat().st_mode) | 0o600)
            if key == "workspace/AGENTS.md":
                current = workspace / "AGENTS.md"
                if current.is_file() and not current.is_symlink():
                    text = current.read_text()
                    start, end = text.find(self.RUNTIME_POLICY_START), text.find(self.RUNTIME_POLICY_END)
                    new_text = frozen.read_text()
                    if start >= 0 and end > start and self.RUNTIME_POLICY_START not in new_text:
                        frozen.write_text(new_text.rstrip() + "\n\n" + text[start:end + len(self.RUNTIME_POLICY_END)] + "\n")
            candidates[key] = frozen
            files[key] = _fingerprint(frozen)
        old_files = previous.get("files", {})
        pending = previous.get("pending_conflicts", {})
        next_pending = {}
        plan, conflicts, overrides = [], [], []
        for key in sorted(set(files) | set(old_files) | set(pending)):
            scope, relative = key.split("/", 1)
            if scope not in roots or _protected(Path(relative)):
                continue
            target = _safe_target(roots[scope], relative)
            before, after, old = _fingerprint(target), files.get(key), old_files.get(key)
            if scope == "common" and after is None:
                continue
            if before == after:
                continue
            # Skills are template capabilities: refresh them even without an old
            # baseline or when the installed copy was modified/deleted locally.
            # The protected-path check above and transactional backups still apply.
            template_skill = scope == "common" or Path(relative).parts[0] == "skills"
            if not template_skill and key not in pending and old is not None and old == after:
                overrides.append(str(target))
                continue
            conflict = not template_skill and (
                key in pending or (old is None and before is not None) or (old is not None and before != old)
            )
            if conflict:
                reason = pending.get(key) or ("no_baseline" if old is None else "locally_modified")
                conflicts.append({"path": str(target), "reason": reason})
                if not force:
                    next_pending[key] = reason
                    continue
            plan.append({"root": roots[scope], "relative": relative, "target": target,
                         "source": candidates.get(key), "before": before, "after": after,
                         "action": "delete" if after is None else "update" if before else "add"})
        package_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        state = {"schema_version": 1, "agent_name": name, "workspace": str(workspace),
                 "config_path": str(self.config_path), "template_name": template_name,
                 "version": version, "package_hash": package_hash, "files": files,
                 "pending_conflicts": next_pending}
        return state, plan, conflicts, {"skipped": False, "template_name": template_name,
            "version_before": previous.get("version"), "version_after": version,
            "baseline_known": bool(previous), "package_hash": package_hash,
            "preserved_paths": preserved, "local_overrides": overrides,
            "common_skills_scope": "environment", "dependencies": dependencies}

    def _verify_refresh_unchanged(self, plan, config_bytes):
        if self.config_path.read_bytes() != config_bytes:
            raise RuntimeError("Config changed during refresh; retry")
        for item in plan:
            _safe_target(item["root"], item["relative"])
            if _fingerprint(item["target"]) != item["before"]:
                raise RuntimeError(f"File changed during refresh; retry: {item['target']}")

    def _backup_refresh(self, backup, plan, config_bytes, config_mode, state_path, previous, new_state, name, job_id):
        _atomic_bytes(backup / "openclaw.json", config_bytes)
        state_existed = state_path.exists()
        if state_existed:
            _atomic_bytes(backup / "state.json", state_path.read_bytes())
        entries = []
        for index, item in enumerate(plan):
            item["backup_file"] = f"file-{index}"
            if item["before"] is not None:
                _atomic_bytes(backup / item["backup_file"], item["target"].read_bytes())
            entries.append({"path": str(item["target"]), "action": item["action"],
                            "before": item["before"], "after": item["after"],
                            "backup_file": item["backup_file"] if item["before"] else None})
        manifest = {"schema_version": 1, "job_id": job_id, "agent_name": name,
                    "config_path": str(self.config_path), "config_mode": config_mode,
                    "state_path": str(state_path), "state_existed": state_existed,
                    "version_before": previous.get("version"),
                    "version_after": new_state.get("version") if new_state else previous.get("version"),
                    "files": entries, "status": "prepared", "memory_preserved": True}
        _private_json(backup / "manifest.json", manifest)
        return manifest

    def _activate_refresh(self):
        env = {"OPENCLAW_CONFIG_PATH": str(self.config_path),
               "OPENCLAW_STATE_DIR": str(self.config_path.parent)}
        self.runner.run([self.bin, "gateway", "restart"], timeout=self.OPENCLAW_COMMAND_TIMEOUT_SECONDS, env_overrides=env)
        status = self.runner.run([self.bin, "gateway", "status", "--require-rpc", "--json"],
                                 timeout=self.SERVER_STATUS_TIMEOUT_SECONDS, env_overrides=env)
        payload = json.loads(status.stdout)
        rpc = payload.get("rpc", {})
        if payload.get("ok") is False or not isinstance(rpc, dict) or rpc.get("ok") is not True:
            raise RuntimeError("Gateway RPC health check did not confirm readiness")

    def _assert_refresh_gateway_scope(self):
        status = self.runner.run([self.bin, "gateway", "status", "--json"],
            timeout=self.SERVER_STATUS_TIMEOUT_SECONDS,
            env_overrides={"OPENCLAW_CONFIG_PATH": str(self.config_path),
                           "OPENCLAW_STATE_DIR": str(self.config_path.parent)})
        payload = json.loads(status.stdout)
        config = payload.get("config", {})
        daemon = config.get("daemon", {})
        daemon_path = daemon.get("path")
        service = payload.get("service", {})
        command = service.get("command") or {}
        installed_env = command.get("environment") or {}
        service_home = Path(installed_env.get("HOME") or str(Path.home())).expanduser()
        service_state = Path(installed_env.get("OPENCLAW_STATE_DIR") or service_home / ".openclaw").expanduser()
        service_config = Path(installed_env.get("OPENCLAW_CONFIG_PATH") or service_state / "openclaw.json").expanduser()
        if (config.get("mismatch") or not isinstance(daemon_path, str)
                or Path(daemon_path).expanduser().resolve() != self.config_path.resolve()
                or not command or service_config.resolve() != self.config_path.resolve()
                or service.get("loaded") is not True):
            raise ValueError("Cannot verify that the installed Gateway service uses this config; refresh without --restart or fix the service mapping")

    def _refresh_model_candidate(self, config):
        if self._codex_models_active():
            return deepcopy(config), {"skipped": True, "reason": "codex_login_active", "scope": "environment"}
        candidate = deepcopy(config)
        providers = config.get("models", {}).get("providers", {})
        managed = {}
        for key, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            try:
                self._model_gateway_for_base_url(str(provider.get("baseUrl", "")))
            except ValueError:
                continue
            managed[key] = provider
        if not managed:
            raise ValueError("No configured Dola model gateway; cannot infer model environment")
        current = self._configured_default_model_from_config(config)
        raw_model = config.get("agents", {}).get("defaults", {}).get("model")
        if not current and isinstance(raw_model, str):
            current = raw_model
        preferred = current.split("/", 1)[0] if isinstance(current, str) else None
        selected = managed.get(preferred) or next((p for key, p in managed.items() if key != self.IMAGE_MODEL_PROVIDER), next(iter(managed.values())))
        base_url = selected["baseUrl"]
        gateway = self._model_gateway_for_base_url(base_url)
        for provider_id, provider in managed.items():
            compatible_image = provider_id == self.IMAGE_MODEL_PROVIDER and self._same_url_host(provider["baseUrl"], base_url)
            if provider["baseUrl"].rstrip("/") != base_url.rstrip("/") and not compatible_image:
                raise ValueError("Multiple Dola model environments/shops configured; refresh would change routing")
        shop = self._ai_shop_for_model_base_url(base_url)
        fetched = self._fetch_supported_gateway_models(self._catalog_url_for_ai_shop(gateway["catalog_url"], shop))
        models = fetched["models"]
        if not models:
            raise ValueError("Refusing an empty model catalog")
        key = selected.get("apiKey")
        if not key:
            raise ValueError("Configured model provider has no apiKey/SecretRef")
        resolved_base = base_url  # Preserve existing environment/shop URL exactly.
        generated = self._models_config_with_api_key(model_key="", models_config=fetched.get("models_config"),
            fallback_base_url=resolved_base, supported_models=models, ai_shop=shop,
            image_base_url=self._image_model_base_url(selected_base_url=resolved_base,
                official_image_model_available=bool(fetched.get("official_image_model_available"))))
        new_providers = generated["providers"]
        for provider_id, value in new_providers.items():
            if provider_id in providers and provider_id not in managed:
                raise FileExistsError(f"Catalog provider conflicts with custom provider: {provider_id}")
            old = managed.get(provider_id, {})
            merged = deepcopy(old)
            merged.update(value)
            merged["apiKey"] = deepcopy(old.get("apiKey") or key)
            new_providers[provider_id] = merged
        custom = {key: deepcopy(value) for key, value in providers.items() if key not in managed}
        all_providers = {**custom, **new_providers}
        available = {self._model_ref(p, definition['id']) for p, provider in all_providers.items()
                     for definition in provider.get("models", []) if isinstance(definition, dict) and definition.get("id")}
        missing = set()

        def check_selected(value):
            if isinstance(value, str) and "/" in value and value.split("/", 1)[0] in managed and value not in available:
                missing.add(value)
            elif isinstance(value, dict):
                for child in value.values():
                    check_selected(child)
            elif isinstance(value, list):
                for child in value:
                    check_selected(child)

        agents = candidate.setdefault("agents", {})
        defaults = agents.setdefault("defaults", {})
        for item in [defaults, *agents.get("list", [])]:
            for field in ("model", "utilityModel", "imageModel", "imageGenerationModel",
                          "videoGenerationModel", "musicGenerationModel", "voiceModel", "pdfModel"):
                check_selected(item.get(field))
        if missing:
            raise ValueError("Selected models absent from refreshed catalog: " + ", ".join(sorted(missing)))
        old_allowlist = defaults.get("models", {})
        allowlist = {ref: value for ref, value in old_allowlist.items() if ref.split("/", 1)[0] not in managed}
        for model in models:
            ref = model["model_ref"]
            allowlist[ref] = deepcopy(old_allowlist.get(ref, {}))
        defaults["models"] = allowlist
        if not defaults.get("model"):
            defaults["model"] = {"primary": self._select_primary_model_ref(models)}
        candidate.setdefault("models", {})["providers"] = all_providers
        return candidate, {"skipped": False, "scope": "environment", "model_count": len(models),
                           "supported_model_refs": [item["model_ref"] for item in models],
                           "current_model_before": current,
                           "current_model_after": self._configured_default_model_from_config(candidate) or current,
                           "ai_shop": shop, "base_url": base_url}
