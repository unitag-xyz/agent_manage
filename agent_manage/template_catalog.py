"""Dola template metadata, independent of the native runtime."""

import json
from http.client import HTTPException
from urllib.parse import quote
from urllib.request import Request, urlopen


def fetch_template_release(origin: str, identify: str) -> dict | None:
    """Look up a release using the caller's trusted Dola origin."""
    endpoint = f"{origin}/vpsmanagement/api/frontend/Template/{quote(identify, safe='')}"
    try:
        with urlopen(Request(endpoint, headers={"Accept": "application/json"}), timeout=15) as response:
            metadata = json.load(response)
    except (OSError, HTTPException, ValueError):
        return None
    release = metadata.get("content") if isinstance(metadata, dict) else None
    if (not isinstance(release, dict) or metadata.get("typeCode") != 1
            or release.get("identify") != identify or not isinstance(release.get("version"), str)
            or not release["version"].strip()):
        return None
    return release
