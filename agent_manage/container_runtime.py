from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import threading
import json

from .agent_hooks import start_agents
from pathlib import Path
from typing import Sequence


NGINX_CONFIG_PATH = Path("/home/node/.openclaw/nginx/nginx.conf")
NGINX_CONFIG_TEMPLATE = Path("/opt/unitag/nginx/nginx.conf")
PUBLIC_ROOT = Path("/home/node/.openclaw/workspace/public")
NGINX_RUNTIME_ROOT = Path("/tmp/nginx")
DEFAULT_NGINX_PORT = 80


def resolve_nginx_port(value: str | None) -> int:
    if value is None or not value.strip():
        return DEFAULT_NGINX_PORT
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("UNITAG_NGINX_PORT must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError("UNITAG_NGINX_PORT must be between 1 and 65535")
    return port


def _nginx_directives(config: str) -> list[tuple[str, tuple[str, ...]]]:
    tokens: list[str] = []
    current: list[str] = []
    index = 0
    quote: str | None = None

    while index < len(config):
        char = config[index]
        if quote is not None:
            if char == "\\" and index + 1 < len(config):
                index += 1
                current.append(config[index])
            elif char == quote:
                quote = None
            else:
                current.append(char)
        elif char in ('"', "'"):
            quote = char
        elif char == "#":
            while index < len(config) and config[index] not in "\r\n":
                index += 1
            continue
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        elif char in "{};":
            if current:
                tokens.append("".join(current))
                current = []
            tokens.append(char)
        else:
            current.append(char)
        index += 1

    if quote is not None:
        raise ValueError("nginx configuration contains an unterminated quoted value")
    if current:
        tokens.append("".join(current))

    directives: list[tuple[str, tuple[str, ...]]] = []
    statement: list[str] = []
    for token in tokens:
        if token == ";":
            if statement:
                directives.append((statement[0].lower(), tuple(statement[1:])))
            statement = []
        elif token in ("{", "}"):
            statement = []
        else:
            statement.append(token)
    return directives


def _directive_arguments(
    directives: list[tuple[str, tuple[str, ...]]], name: str
) -> list[tuple[str, ...]]:
    return [arguments for directive, arguments in directives if directive == name.lower()]


def validate_nginx_config(config: str, expected_port: int) -> None:
    directives = _nginx_directives(config)
    if _directive_arguments(directives, "alias"):
        raise ValueError("nginx alias may not bypass the public workspace root")

    includes = _directive_arguments(directives, "include")
    if includes != [("/etc/nginx/mime.types",)]:
        raise ValueError("nginx may only include /etc/nginx/mime.types")

    public_root = str(PUBLIC_ROOT).replace("\\", "/")
    runtime_root = str(NGINX_RUNTIME_ROOT).replace("\\", "/")
    roots = _directive_arguments(directives, "root")
    if roots != [(public_root,)]:
        raise ValueError(f"nginx root must be exactly {public_root}")

    listens = _directive_arguments(directives, "listen")
    if len(listens) != 1 or not listens[0] or listens[0][0] != str(expected_port):
        raise ValueError(f"nginx must listen exactly once on port {expected_port}")

    required_paths = (
        f"pid {runtime_root}/nginx.pid;",
        f"error_log {runtime_root}/log/error.log",
        f"access_log {runtime_root}/log/access.log",
        f"client_body_temp_path {runtime_root}/client_temp;",
        f"proxy_temp_path {runtime_root}/proxy_temp;",
        f"fastcgi_temp_path {runtime_root}/fastcgi_temp;",
        f"uwsgi_temp_path {runtime_root}/uwsgi_temp;",
        f"scgi_temp_path {runtime_root}/scgi_temp;",
    )
    if any(item not in config for item in required_paths):
        raise ValueError("nginx runtime files must remain under /tmp/nginx")
    if "disable_symlinks on;" not in config or "autoindex off;" not in config:
        raise ValueError("nginx directory listing and symlink serving must remain disabled")


def initialize_nginx_config(port: int) -> Path:
    PUBLIC_ROOT.mkdir(parents=True, exist_ok=True)
    NGINX_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    for relative in ("log", "client_temp", "proxy_temp", "fastcgi_temp", "uwsgi_temp", "scgi_temp"):
        (NGINX_RUNTIME_ROOT / relative).mkdir(parents=True, exist_ok=True)

    if not NGINX_CONFIG_PATH.exists():
        template = NGINX_CONFIG_TEMPLATE.read_text(encoding="utf-8")
        rendered = template.replace("__UNITAG_NGINX_PORT__", str(port))
        NGINX_CONFIG_PATH.write_text(rendered, encoding="utf-8")

    config = NGINX_CONFIG_PATH.read_text(encoding="utf-8")
    validate_nginx_config(config, port)
    return NGINX_CONFIG_PATH


def _render_fallback_nginx_config(port: int) -> Path:
    template = NGINX_CONFIG_TEMPLATE.read_text(encoding="utf-8")
    rendered = template.replace("__UNITAG_NGINX_PORT__", str(port))
    validate_nginx_config(rendered, port)
    fallback_path = NGINX_RUNTIME_ROOT / "fallback-nginx.conf"
    fallback_path.write_text(rendered, encoding="utf-8")
    return fallback_path


def _test_nginx_config(config_path: Path) -> None:
    subprocess.run(
        ["nginx", "-t", "-p", f"{NGINX_RUNTIME_ROOT}/", "-c", str(config_path)],
        check=True,
    )


def select_nginx_config(port: int) -> Path:
    try:
        config_path = initialize_nginx_config(port)
        _test_nginx_config(config_path)
        return config_path
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(
            "nginx persistent configuration rejected; "
            f"using trusted runtime fallback without modifying it: {exc}",
            file=sys.stderr,
        )

    fallback_path = _render_fallback_nginx_config(port)
    _test_nginx_config(fallback_path)
    return fallback_path


def _terminate(process: subprocess.Popen[bytes], timeout: float = 10.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_container(command: Sequence[str], port: int) -> int:
    if not command:
        raise ValueError("OpenClaw command is required")
    config_path = select_nginx_config(port)

    nginx = subprocess.Popen(
        ["nginx", "-p", f"{NGINX_RUNTIME_ROOT}/", "-c", str(config_path), "-g", "daemon off;"]
    )
    openclaw = subprocess.Popen(list(command))
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    cancel_hooks = threading.Event()
    def restore_services():
        path = Path(os.environ.get("OPENCLAW_CONFIG_PATH", "/home/node/.openclaw/openclaw.json"))
        if not path.is_file():
            return
        try:
            report = start_agents(path, cancel=cancel_hooks)
            print("agent services: " + json.dumps(report, ensure_ascii=False), file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"agent services: startup failed ({type(exc).__name__})", file=sys.stderr, flush=True)
    hook_thread = threading.Thread(target=restore_services, name="agent-start-hooks", daemon=True)
    hook_thread.start()
    try:
        while not stopping:
            nginx_code = nginx.poll()
            openclaw_code = openclaw.poll()
            if nginx_code is not None:
                return nginx_code if nginx_code != 0 else 1
            if openclaw_code is not None:
                return openclaw_code
            time.sleep(0.2)
        return 0
    finally:
        cancel_hooks.set()
        hook_thread.join(timeout=5)
        _terminate(openclaw)
        _terminate(nginx)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unitag-container-runtime")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    port = resolve_nginx_port(os.environ.get("UNITAG_NGINX_PORT"))
    return run_container(command, port)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"container runtime failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
