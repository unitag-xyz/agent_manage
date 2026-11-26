"""Compatibility entry point for the detached OpenClaw login worker."""

import sys

from .adapters.openclaw.codex_auth import run_login_worker

if __name__ == "__main__":
    run_login_worker(*sys.argv[1:])
