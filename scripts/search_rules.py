"""Shared search and model-exclusion rules for both collectors."""
from __future__ import annotations
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUERY_FILE = ROOT / "search_queries.txt"

def load_search_queries() -> list[str]:
    if not QUERY_FILE.exists():
        return []
    return [line.strip() for line in QUERY_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]

_QD_RE = re.compile(r"\b(?:27|32)\s*qd\s*-?\s*10\b", re.I)
_PRO_RE = re.compile(r"\bpro\b", re.I)

def should_exclude_text(*values: str | None) -> bool:
    text = "\n".join(value or "" for value in values)
    return bool(_QD_RE.search(text) and not _PRO_RE.search(text))
