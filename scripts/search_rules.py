"""Shared search and relevance rules for both collectors."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUERY_FILE = ROOT / "search_queries.txt"

def load_search_queries() -> list[str]:
    if not QUERY_FILE.exists():
        return []
    return [
        line.strip()
        for line in QUERY_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

# These are the monitor models/families relevant to this investigation.
_MODEL_RE = re.compile(r"\b(?:27|32)\s*(?:ud|q|qd)\s*-?\s*10\b", re.I)
_QD_RE = re.compile(r"\b(?:27|32)\s*qd\s*-?\s*10\b", re.I)
_PRO_RE = re.compile(r"\bpro\b", re.I)
_BRAND_RE = re.compile(r"\b(?:lenovo|legion)\b", re.I)
_MONITOR_RE = re.compile(r"\b(?:monitor|monitors|display|displays|screen|screens)\b", re.I)
_UNRELATED_DEVICE_RE = re.compile(
    r"\b(?:laptop|notebook|tablet|phone|smartphone|handheld|legion\s+go|y700)\b",
    re.I,
)

def should_exclude_text(*values: str | None) -> bool:
    """Exclude 27QD/32QD results unless the same result identifies Pro."""
    text = "\n".join(value or "" for value in values)
    return bool(_QD_RE.search(text) and not _PRO_RE.search(text))


def is_relevant_monitor_text(*values: str | None) -> bool:
    """Require a target model, or Lenovo/Legion plus explicit monitor context.

    Generic OLED/HDR/brightness terms never qualify by themselves. Results
    clearly about laptops, tablets, phones, or handhelds are rejected unless
    they explicitly name one of the target monitor models.
    """
    text = "\n".join(value or "" for value in values)
    if should_exclude_text(text):
        return False
    if _MODEL_RE.search(text):
        return True
    if _UNRELATED_DEVICE_RE.search(text):
        return False
    return bool(_BRAND_RE.search(text) and _MONITOR_RE.search(text))
