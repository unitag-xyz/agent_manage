"""Read-only physical transcript segments for a logical OpenClaw thread."""
from __future__ import annotations

import re

from .version import implementation, load_implementation, package_directory, package_version
from ...segment_timing import CURRENT, measured_read


MAX_REFERENCE_CHARS = 8192


class SegmentManagementMixin:
    def segment_history(self, session_key, session_id, *, edge=None, limit=50, cursor=None, view=None, omit_thinking=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if edge is not None and cursor is not None:
            raise ValueError("edge and cursor are mutually exclusive")
        if edge is not None and edge not in ("start", "end"):
            raise ValueError("edge must be start or end")
        if view is not None and view not in ("full", "compact"):
            raise ValueError("view must be full or compact")
        if omit_thinking is not None and not isinstance(omit_thinking, bool):
            raise ValueError("omit_thinking must be a boolean")
        return self._segment_bridge("history", session_key, session_id,
                                    edge=edge or "end", limit=limit, cursor=cursor, view=view, omitThinking=omit_thinking)

    def segment_message_get(self, session_key, session_id, message_id, *, ref=None, cursor=None):
        return self._segment_bridge("message", session_key, session_id,
                                    messageId=message_id, ref=ref, cursor=cursor)

    def segment_media_get(self, session_key, session_id, ref=None, *, cursor=None):
        if ref is None and cursor is None:
            raise ValueError("media read requires ref or cursor")
        return self._segment_bridge("media", session_key, session_id, ref=ref, cursor=cursor)

    @measured_read
    def _segment_bridge(self, action, session_key, session_id, **params):
        if not isinstance(session_key, str) or not re.fullmatch(r"agent:[a-zA-Z0-9_-]+:.+", session_key):
            raise ValueError("session-key must be a full agent session key")
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", session_id):
            raise ValueError("Invalid physical session ID")
        if action == "message" and (not isinstance(params["messageId"], str) or not params["messageId"]):
            raise ValueError("message-id is required")
        for key in ("cursor", "ref"):
            value = params.get(key)
            if value is not None and (not isinstance(value, str) or not value or len(value) > MAX_REFERENCE_CHARS):
                raise ValueError(f"Invalid {key}")
        with CURRENT.get().span("environmentAndVersionMs"):
            env = self.runner._command_env()
            package = package_directory(self.bin, path=env.get("PATH"))
            if package is None:
                raise FileNotFoundError("OpenClaw is required for segment history")
            family = implementation(package_version(package), "history")
            if family is None:
                raise ValueError("Segment history currently supports server OpenClaw 2026.7.x only")
        with CURRENT.get().span("configReadMs"):
            config = self._load_config()
        payload = {"action": action, "sessionKey": session_key, "sessionId": session_id,
                   "config": config, "root": str(self.config_path.parent), **params}
        return load_implementation(family, "segment_reader").read_segment(payload, env)
