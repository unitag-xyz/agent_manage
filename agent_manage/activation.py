"""Fly-only configuration publication; effective activation belongs to the supervisor."""

from __future__ import annotations

import copy
import errno
import hashlib
import os
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from time import perf_counter

from .models import CreateInstanceRequest
from .response import redact_sensitive_values


class FlyActivationMixin:
    @staticmethod
    def require_fly_activation() -> None:
        if (os.environ.get("UNITAG_AGENT_MANAGER_RUNTIME") != "container"
                or os.environ.get("UNITAG_AGENT_MANAGER_ACTIVATION_MODE") != "fly"):
            raise ValueError(
                "activate-instance requires UNITAG_AGENT_MANAGER_RUNTIME=container "
                "and UNITAG_AGENT_MANAGER_ACTIVATION_MODE=fly"
            )

    def activate_instance(self, request: CreateInstanceRequest) -> dict:
        self.require_fly_activation()
        if request.agent_zip or request.template_name or request.local or request.model:
            raise ValueError("activate-instance only configures existing agents; no template, zip, model override or local mode")
        self.runner.add_redaction_value(request.model_key.strip())
        started_at = perf_counter()
        if self.runner.dry_run:
            result = self._configure_existing_instance(request)
            result.pop("restart_required", None)
            return {**result, "activationRequired": False, "requestedConfigSha256": None,
                    "dryRun": True, "total_elapsed_ms": self._elapsed_ms(started_at)}

        config_path = self.config_path
        backup_path = config_path.with_suffix(config_path.suffix + ".bak")
        lock_path = config_path.with_name(f".{config_path.name}.activation.lock")
        steps = []
        stage_path = None
        locks = ExitStack()
        config_published = False
        try:
            # Serialize this new command only. Other writers are detected by the snapshot check.
            locks.enter_context(self._activation_lock(lock_path))
            snapshot_started_at = perf_counter()
            snapshot = self._activation_snapshot(config_path)
            steps.append(self._build_step_payload(
                "activation.snapshot", {"config_path": str(config_path)},
                elapsed_ms=self._elapsed_ms(snapshot_started_at),
            ))
            stage_started_at = perf_counter()
            stage_path = self._activation_stage(config_path, snapshot[0])
            steps.append(self._build_step_payload(
                "activation.stage", {"config_path": str(config_path)},
                elapsed_ms=self._elapsed_ms(stage_started_at),
            ))
            staged = copy.copy(self)
            staged.config_path = stage_path
            result = staged._configure_existing_instance(replace(request, rollback_on_fail=False))
            result.pop("restart_required", None)
            steps.extend(result["steps"])
            final_bytes = stage_path.read_bytes()
            digest = hashlib.sha256(final_bytes).hexdigest()

            def commit():
                nonlocal config_published
                previous_backup = self._activation_snapshot(backup_path) if backup_path.exists() else None
                backup_stage = self._activation_stage(backup_path, snapshot[0])
                backup_written = False
                try:
                    self._activation_check_snapshot(config_path, snapshot)
                    os.replace(backup_stage, backup_path)
                    backup_written = True
                    self._activation_check_snapshot(config_path, snapshot)
                    os.replace(stage_path, config_path)
                    config_published = True
                except Exception:
                    if backup_written:
                        if previous_backup is None:
                            backup_path.unlink(missing_ok=True)
                        else:
                            restored = self._activation_stage(backup_path, previous_backup[0])
                            try:
                                os.chmod(restored, previous_backup[1])
                                os.replace(restored, backup_path)
                            finally:
                                restored.unlink(missing_ok=True)
                    raise
                finally:
                    backup_stage.unlink(missing_ok=True)
                return {"config_path": str(config_path), "backup_path": str(backup_path)}

            self._run_timed_step(steps, "activation.commit", commit)
            result.update(steps=steps, total_elapsed_ms=self._elapsed_ms(started_at),
                          activationRequired=True, requestedConfigSha256=digest)
            # Success data contains no model key; substring redaction would corrupt hashes/paths.
            return self._activation_public_paths(result, stage_path, config_path)
        except Exception as exc:
            payload = self._embedded_error_payload(exc)
            steps.extend(payload.get("steps", []))
            secrets = [request.model_key, request.model_key.strip()] if request.model_key.strip() else []
            if stage_path is not None:
                steps = self._activation_public_paths(steps, stage_path, config_path)
            reason = redact_sensitive_values(payload.get("error", str(exc)), secrets)
            if stage_path is not None:
                reason = self._activation_public_paths(reason, stage_path, config_path)
            error = self._create_instance_failure(
                RuntimeError(reason),
                redact_sensitive_values(steps, secrets),
                [{"step": "rollback.activation.discard", "result": {"config_published": config_published}}],
                started_at,
            )
            raise error from exc
        finally:
            try:
                if stage_path is not None:
                    stage_path.unlink(missing_ok=True)
                    stage_path.with_suffix(stage_path.suffix + ".bak").unlink(missing_ok=True)
            finally:
                locks.close()

    @staticmethod
    @contextmanager
    def _activation_lock(path: Path):
        # Keep the inode: unlinking a locked file would allow a second independent lock.
        if path.is_symlink():
            raise RuntimeError("Unsafe activation lock file")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.set_inheritable(descriptor, False)

            def check_identity():
                opened = os.fstat(descriptor)
                named = path.lstat()
                if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode)
                        or opened.st_nlink != 1
                        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                        or (os.name == "posix" and (opened.st_uid != os.geteuid()
                                                   or opened.st_mode & 0o077))):
                    raise RuntimeError("Unsafe activation lock file")

            check_identity()
            try:
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif os.name == "nt":
                    import msvcrt
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    raise RuntimeError("Activation locking is unsupported on this platform")
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise RuntimeError("Activation configuration lock is busy") from exc
                raise
            check_identity()
            yield
        finally:
            # Descriptor-scoped locks are also released by the OS on kill/timeout.
            os.close(descriptor)

    @staticmethod
    def _activation_snapshot(path: Path) -> tuple[bytes, int, int, int, int]:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            data = handle.read()
            after = os.fstat(handle.fileno())
        if (before.st_ino, before.st_mtime_ns, before.st_size, before.st_mode) != (
                after.st_ino, after.st_mtime_ns, after.st_size, after.st_mode):
            raise RuntimeError("Concurrent configuration write detected")
        return data, after.st_mode & 0o777, after.st_ino, after.st_mtime_ns, after.st_size

    def _activation_check_snapshot(self, path: Path, snapshot: tuple) -> None:
        if self._activation_snapshot(path) != snapshot:
            raise RuntimeError("Concurrent configuration write detected; activation not published")

    @staticmethod
    def _activation_stage(path: Path, data: bytes) -> Path:
        staged_path = None
        try:
            with tempfile.NamedTemporaryFile("wb", prefix=".activation-", suffix=".json",
                                             dir=path.parent, delete=False) as handle:
                staged_path = Path(handle.name)
                os.chmod(staged_path, 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            return staged_path
        except Exception:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
            raise

    @classmethod
    def _activation_public_paths(cls, value, stage_path: Path, config_path: Path):
        if isinstance(value, dict):
            return {key: cls._activation_public_paths(item, stage_path, config_path)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [cls._activation_public_paths(item, stage_path, config_path) for item in value]
        if isinstance(value, str):
            return value.replace(str(stage_path), str(config_path))
        return value
