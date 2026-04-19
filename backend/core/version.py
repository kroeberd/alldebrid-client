from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def read_version() -> str:
    for candidate in (
        Path("/app/VERSION"),
        Path(__file__).resolve().parents[2] / "VERSION",
    ):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()
    return "unknown"

