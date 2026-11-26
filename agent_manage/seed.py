"""Compatibility entry point for existing OpenClaw images."""

from .adapters.openclaw.seed import assert_seed_has_no_runtime_secrets, initialize_openclaw_seed, main

__all__ = ["assert_seed_has_no_runtime_secrets", "initialize_openclaw_seed", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
