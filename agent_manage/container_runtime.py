"""Compatibility entry point for existing OpenClaw images."""

from .adapters.openclaw.container_runtime import main, run_container, select_nginx_config

__all__ = ["main", "run_container", "select_nginx_config"]

if __name__ == "__main__":
    raise SystemExit(main())
