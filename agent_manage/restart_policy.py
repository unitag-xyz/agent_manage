"""Compatibility import for existing Host OpenClaw validation tooling."""

from .adapters.openclaw.v2026_7.restart_policy import host_restart_required, standard_host_reload_available

__all__ = ["host_restart_required", "standard_host_reload_available"]
