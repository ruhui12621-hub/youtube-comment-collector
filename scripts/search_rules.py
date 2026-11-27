"""Shared search and source-screening rules for both collectors."""
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


# Target monitor model families. Keep Yoga-branded variants when the exact
# target monitor model is present; do not reject them by product-family name.
_MODEL_RE = re.compile(r"\b(?:27|32)\s*(?:ud|q|qd)\s*-?\s*10\b", re.I)
_QD_RE = re.compile(r"\b(?:27|32)\s*qd(?:\s*-?\s*10)?\b", re.I)
_PRO_RE = re.compile(r"\bpro\b", re.I)
_BRAND_RE = re.compile(r"\b(?:lenovo|legion)\b", re.I)
_MONITOR_RE = re.compile(r"\b(?:monitor|monitors|display|displays)\b", re.I)
_UNRELATED_DEVICE_RE = re.compile(
    r"\b(?:laptop|notebook|tablet|phone|smartphone|handheld|"
    r"gaming\s+pc|legion\s+go|y700|ipad)\b",
    re.I,
)


def should_exclude_text(*values: str | None) -> bool:
    """Exclude 27QD/32QD results unless the same source identifies Pro."""
    text = "\n".join(value or "" for value in values)
    return bool(_QD_RE.search(text) and not _PRO_RE.search(text))


def is_relevant_monitor_text(*values: str | None) -> bool:
    """Screen a source/video/thread, not individual comment wording.

    Exact target model mentions take precedence over product-family words
    (e.g. Yoga Pro 27UD-10 is still a target monitor). Otherwise require
    Lenovo/Legion monitor context and reject laptop/tablet/phone/handheld
    sources. Generic OLED/HDR content alone is never sufficient.
    """
    text = "\n".join(value or "" for value in values)
    if should_exclude_text(text):
        return False
    has_monitor_context = bool(_MONITOR_RE.search(text))
    has_legion_pro_context = bool(_LEGION_PRO_RE.search(text))

    # Do not let a matching model number alone pull in laptops or other
    # Lenovo product families. A clear monitor reference or Legion Pro
    # branding is required for exact-model matches.
    if _UNRELATED_DEVICE_RE.search(text) and not has_monitor_context:
        return False
    if _OTHER_LENOVO_FAMILY_RE.search(text) and not has_legion_pro_context:
        return False
    if _MODEL_RE.search(text):
        return has_monitor_context or has_legion_pro_context
    if _UNRELATED_DEVICE_RE.search(text):
        return False
    return bool(_BRAND_RE.search(text) and has_monitor_context)
