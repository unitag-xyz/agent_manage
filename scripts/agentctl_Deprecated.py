#!/usr/bin/env python3
"""Deprecated v1 entrypoint. Use scripts/agentctl.py instead."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openclaw_remote_Deprecated.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
