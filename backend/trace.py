"""TraceStep dataclass + helpers for the methodology waterfall.

Every spec function in api.spec has a sibling trace_* wrapper that records the
inputs, intermediate values, branch taken, and final result as a TraceStep.
The /methodology/* endpoints return ordered lists of these.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class TraceStep:
    id: str
    function: str
    spec_file: str
    spec_lines: tuple[int, int]
    spec_excerpt: str
    inputs: dict[str, Any] = field(default_factory=dict)
    substituted: str = ""
    branches: list[str] = field(default_factory=list)
    intermediate: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    refreshed_at: str = ""
    # Provenance: "live" (read from Beacon REST) | "derived" (re-emulated from spec)
    #           | "computed" (pure function output) | "constant" (spec preset)
    provenance: str = "computed"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["spec_lines"] = list(self.spec_lines)
        return d


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gwei_to_eth(g: int) -> float:
    return round(g / 1_000_000_000, 4)
