"""Device-code SDK shipped with the July OpenClaw package."""

from pathlib import Path


def bridge_path(package: Path, version: str) -> Path:
    for relative in ("dist/plugin-sdk/provider-auth.js", "dist/extensions/openai/openai-chatgpt-device-code.js"):
        if not (package / relative).is_file():
            raise ValueError(f"Server OpenClaw {version} is missing the required Codex SDK module: {relative}")
    return Path(__file__).with_name("codex_auth_bridge.mjs")
