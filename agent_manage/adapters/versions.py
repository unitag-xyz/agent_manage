"""Select an implementation before execution, never after a native failure."""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class VersionFamily:
    name: str
    matches: Callable[[str], bool]
    capabilities: frozenset[str]


def select_family(families, version: str, capability: str) -> str | None:
    matches = [family.name for family in families
               if capability in family.capabilities and family.matches(version)]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous implementations for {capability} at version {version}")
    return matches[0] if matches else None
