from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from ...file_io import atomic_write_bytes
from ...skill_permissions import prepare_shared_skill_path


INITIALIZING_MARKER = ".unitag-seed-initializing"
INITIALIZED_MARKER = ".unitag-seed-initialized"
SECRET_FILE_NAMES = {"auth-profiles.json", "secrets.json"}
SECRET_KEY_NAMES = {"apikey", "password", "secret", "token"}


def assert_seed_has_no_runtime_secrets(seed_dir: Path) -> None:
    seed_dir = seed_dir.resolve()
    if not seed_dir.is_dir():
        raise FileNotFoundError(f"OpenClaw seed is missing: {seed_dir}")
    secret_files = [path for path in seed_dir.rglob("*") if path.name in SECRET_FILE_NAMES]
    if secret_files:
        raise ValueError(f"Secret-bearing file found in OpenClaw seed: {secret_files[0].name}")

    config_path = seed_dir / "openclaw.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"OpenClaw seed config is missing: {config_path}")
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    def visit(value: object, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                next_path = (*path, str(key))
                if str(key).lower() in SECRET_KEY_NAMES and item not in (None, "", [], {}):
                    raise ValueError(
                        f"Runtime secret found in OpenClaw seed config: {'.'.join(next_path)}"
                    )
                visit(item, next_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, (*path, str(index)))

    visit(payload)


def _deep_merge_seed_with_runtime(seed_value: object, runtime_value: object) -> object:
    """Merge a runtime config over its secret-free image seed base.

    Dictionaries are merged recursively so an early DockerManager write such as
    ``gateway.auth.token`` does not remove the pre-registered ``agents`` tree.
    Lists and scalar values intentionally use the runtime value as-is: they are
    user/runtime-owned OpenClaw configuration, not seed defaults.
    """
    if isinstance(seed_value, dict) and isinstance(runtime_value, dict):
        merged = dict(seed_value)
        for key, value in runtime_value.items():
            merged[key] = _deep_merge_seed_with_runtime(merged[key], value) if key in merged else value
        return merged
    return runtime_value


def _merged_runtime_config(seed_config_path: Path, runtime_config_path: Path) -> str:
    seed_config = json.loads(seed_config_path.read_text(encoding="utf-8"))
    if not isinstance(seed_config, dict):
        raise ValueError(f"OpenClaw seed config must be a JSON object: {seed_config_path}")
    if not runtime_config_path.is_file():
        return json.dumps(seed_config, ensure_ascii=False, indent=2) + "\n"

    runtime_config = json.loads(runtime_config_path.read_text(encoding="utf-8"))
    if not isinstance(runtime_config, dict):
        raise ValueError(f"Runtime OpenClaw config must be a JSON object: {runtime_config_path}")
    merged = _deep_merge_seed_with_runtime(seed_config, runtime_config)
    return json.dumps(merged, ensure_ascii=False, indent=2) + "\n"


def _atomic_write_runtime_config(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def initialize_openclaw_seed(seed_dir: Path, target_dir: Path) -> dict[str, object]:
    seed_dir = seed_dir.resolve()
    target_dir = target_dir.resolve()
    if not seed_dir.is_dir() or not any(seed_dir.iterdir()):
        raise FileNotFoundError(f"OpenClaw seed is missing or empty: {seed_dir}")
    assert_seed_has_no_runtime_secrets(seed_dir)

    target_dir.mkdir(parents=True, exist_ok=True)
    initializing = target_dir / INITIALIZING_MARKER
    initialized = target_dir / INITIALIZED_MARKER
    if initialized.exists():
        return {
            "initialized": False,
            "reason": "already_initialized",
            "target": str(target_dir),
        }

    initializing.touch(exist_ok=True)
    copied: list[str] = []
    try:
        for source in sorted(seed_dir.iterdir(), key=lambda item: item.name):
            if source.name == "openclaw.json":
                continue
            destination = target_dir / source.name
            if source.name == "skills":
                prepare_shared_skill_path(destination, target_dir)
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(source, destination)
            if source.name == "skills":
                prepare_shared_skill_path(destination, target_dir)
            copied.append(source.name)
        runtime_config_path = target_dir / "openclaw.json"
        _atomic_write_runtime_config(
            runtime_config_path,
            _merged_runtime_config(seed_dir / "openclaw.json", runtime_config_path),
        )
        copied.append("openclaw.json")
        initialized.write_text("1\n", encoding="ascii")
    finally:
        initializing.unlink(missing_ok=True)

    return {
        "initialized": True,
        "target": str(target_dir),
        "copied": copied,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="unitag-openclaw-seed")
    parser.add_argument("--seed", default="/opt/unitag/openclaw-seed")
    parser.add_argument("--target", default="/home/node/.openclaw")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        assert_seed_has_no_runtime_secrets(Path(args.seed))
        return 0
    initialize_openclaw_seed(Path(args.seed), Path(args.target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
