"""Recover only owned local refresh writes; external hooks are not reversible."""
import hashlib
import json
from pathlib import Path

from .errors import APIError
from ..file_io import atomic_write_bytes, fingerprint, write_private_json, sync_directory


def recovery_entries(config_path):
    root = Path(config_path).parent / "agent-manage" / "backups"
    if root.is_symlink():
        raise ValueError("Backup directory must not be a symlink")
    for path in sorted(root.glob("*/manifest.json")):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("Backup journal must not be a symlink")
        data = json.loads(path.read_text())
        if data.get("config_path") == str(config_path) and data.get("status") in ("prepared", "rollback_failed", "recovery_required"):
            yield path, data


def recover_refreshes(config_path):
    """Caller holds runtime writer lock. Idempotent after a second interruption."""
    recovered = []
    for manifest_path, manifest in recovery_entries(config_path):
        entries = manifest.get("recovery_entries")
        conflicts = []
        if entries is None:
            raise APIError("RECOVERY_REQUIRED", "An older interrupted refresh needs manual recovery",
                           details={"backupId": manifest_path.parent.name})
        # Preflight everything before restoring anything; don't overwrite an independent edit.
        for item in entries:
            path = Path(item["path"])
            if any(parent.is_symlink() for parent in [path, *path.parents]):
                conflicts.append(str(path))
                continue
            if fingerprint(path) not in (item["before"], item["after"]):
                conflicts.append(str(path))
            if item["before"] is not None:
                backup = manifest_path.parent / item["backup_file"]
                if backup.is_symlink() or backup.parent != manifest_path.parent or not backup.is_file() or hashlib.sha256(backup.read_bytes()).hexdigest() != item["before"]["sha256"]:
                    conflicts.append(str(path))
        if conflicts:
            manifest.update(status="recovery_required", rollback_errors=conflicts)
            write_private_json(manifest_path, manifest)
            raise APIError("RECOVERY_REQUIRED", "Interrupted refresh conflicts with current files",
                           details={"backupId": manifest_path.parent.name, "conflicts": conflicts})
        for item in reversed(entries):
            path = Path(item["path"])
            if fingerprint(path) == item["before"]:
                continue
            if item["before"] is None:
                path.unlink(missing_ok=True)
                sync_directory(path.parent)
            else:
                atomic_write_bytes(path, (manifest_path.parent / item["backup_file"]).read_bytes(), item["before"]["mode"])
        manifest.update(status="rolled_back", recovery="interrupted_refresh", runtime_restored=None)
        write_private_json(manifest_path, manifest)
        recovered.append(manifest_path.parent.name)
    return recovered
