#!/usr/bin/env python3
"""Build a normalized, date-queryable analysis index from the raw archives.

Raw per-video/per-thread files remain the source of truth. This script only
creates derived indexes for fast recent-window and monthly analysis.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
COMMENTS = DATA / "comments"
FORUM = DATA / "lenovo_forum"
OUT = DATA / "analysis"
NOW = datetime.now(timezone.utc)
RELATIVE_RE = re.compile(
    r"^\s*(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago\s*$", re.I
)
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+-]+Z?)?$")
# Allow inconsistent spacing around the hyphen (e.g. "27Q- 10").
EXCLUDED_MODEL_RE = re.compile(r"\b(?:27|32)\s?qd\s*-?\s*10\b", re.I)
Q_MODEL_RE = re.compile(r"\b27\s?q\s*-?\s*10\b", re.I)
UD_MODEL_RE = re.compile(r"\b(?:27|32)\s?ud\s*-?\s*10\b", re.I)
MODEL_RE = re.compile(r"\b(?:27|32)\s?(?:ud|qd|q)\s*-?\s*10\b", re.I)
FAMILY_RE = re.compile(r"\blegion\b.*\b(?:pro|oled)\b|\b(?:pro|oled)\b.*\blegion\b", re.I | re.S)
ISSUE_RE = re.compile(
    r"\b(hdr|dimm?ing|brightness|bright|dark|dim|abl|firmware|"
    r"oled care|screen dims|brightness drops)\b",
    re.I,
)
CN_ISSUE_RE = re.compile(r"(变暗|亮度|降亮度|暗屏|亮度下降|亮度降低)")
STRONG_ISSUE_RE = re.compile(
    r"(dimm?ing|brightness drops|screen dims|变暗|亮度下降|亮度降低|暗屏|"
    r"30[-– ]?40\s*minutes|30[-– ]?40\s*分钟)", re.I
)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def parse_time(value: Any, reference: datetime | None = None) -> tuple[str | None, str]:
    """Return normalized UTC time and quality; never silently call estimates exact."""
    if value is None or value == "":
        return None, "unknown"
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            if number > 100_000_000_000:
                number /= 1000
            return iso(datetime.fromtimestamp(number, timezone.utc)), "exact"
        except (OverflowError, OSError, ValueError):
            return None, "unknown"
    text = str(value).strip()
    if not text:
        return None, "unknown"
    if re.fullmatch(r"\d{9,13}", text):
        try:
            number = int(text)
            if len(text) == 13:
                number /= 1000
            return iso(datetime.fromtimestamp(number, timezone.utc)), "exact"
        except (OverflowError, OSError, ValueError):
            pass
    if ISO_RE.match(text):
        candidate = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return iso(dt), "exact"
        except ValueError:
            pass
    match = RELATIVE_RE.match(text)
    if match and reference:
        # Archive metadata stores collection timestamps as ISO strings.
        # Normalize them here before applying relative YouTube labels.
        if isinstance(reference, str):
            reference_text = reference.strip().replace("Z", "+00:00")
            try:
                reference = datetime.fromisoformat(reference_text)
                if reference.tzinfo is None:
                    reference = reference.replace(tzinfo=timezone.utc)
                else:
                    reference = reference.astimezone(timezone.utc)
            except ValueError:
                reference = None
        if reference is None:
            return None, "unknown"
        amount = int(match.group(1))
        unit = match.group(2).lower()
        days = {
            "minute": amount / 1440,
            "hour": amount / 24,
            "day": amount,
            "week": amount * 7,
            "month": amount * 30,
            "year": amount * 365,
        }[unit]
        return iso(reference - timedelta(days=days)), "relative_estimate"
    return None, "unknown"


def relevance(title: str, text: str) -> tuple[str, list[str], str | None]:
    combined = f"{title}\n{text}"
    models = sorted(set(m.group(0).upper().replace(" ", "").replace("-", "") for m in MODEL_RE.finditer(combined)))
    has_ud = bool(UD_MODEL_RE.search(combined))
    has_q = bool(Q_MODEL_RE.search(combined))
    has_qd = bool(EXCLUDED_MODEL_RE.search(combined))
    if has_qd and not re.search(r"\bpro\b", combined, re.I):
        return "excluded", models, "27QD-10 or 32QD-10 appears without Pro context"
    has_pro = bool(re.search(r"\bpro\b", combined, re.I))
    if (has_ud or (has_q and has_pro)) and STRONG_ISSUE_RE.search(combined):
        return "high", models, None
    if (has_ud or (has_q and has_pro)) and (ISSUE_RE.search(combined) or CN_ISSUE_RE.search(combined)):
        return "medium", models, None
    if (ISSUE_RE.search(combined) or CN_ISSUE_RE.search(combined)) and ("oled" in combined.lower() or "lenovo" in combined.lower() or FAMILY_RE.search(combined)):
        return "low", models, None
    return "low", models, None


def add_record(records: list[dict[str, Any]], record: dict[str, Any]) -> None:
    title = record.get("source_title") or ""
    text = record.get("text") or ""
    level, models, reason = relevance(title, text)
    record["relevance"] = level
    record["models_mentioned"] = models
    if reason:
        record["exclusion_reason"] = reason
    records.append(record)


def collect_youtube(records: list[dict[str, Any]]) -> dict[str, int]:
    stats = {"videos": 0, "comments": 0}
    for path in sorted(COMMENTS.glob("*.json")):
        archive = read_json(path, {})
        if not isinstance(archive, dict):
            continue
        stats["videos"] += 1
        video_id = str(archive.get("video_id") or path.stem)
        title = archive.get("title") or ""
        url = archive.get("url") or f"https://www.youtube.com/watch?v={video_id}"
        fetched_at, _ = parse_time(archive.get("last_collected_at"))
        for comment in archive.get("comments") or []:
            if not isinstance(comment, dict):
                continue
            stats["comments"] += 1
            published, quality = parse_time(comment.get("published_at") or comment.get("published"), fetched_at)
            first_seen, first_quality = parse_time(comment.get("first_seen_at"))
            first_quality = comment.get("first_seen_time_quality") or first_quality
            last_seen, _ = parse_time(comment.get("last_seen_at") or archive.get("last_collected_at"))
            item = {
                "record_id": f"youtube:{comment.get('comment_id')}",
                "platform": "youtube",
                "record_type": "reply" if comment.get("is_reply") else "comment",
                "source_title": title,
                "source_url": url,
                "video_id": video_id,
                "comment_id": str(comment.get("comment_id") or ""),
                "parent_id": comment.get("parent_id"),
                "author": comment.get("author"),
                "text": comment.get("text") or "",
                "published_raw": comment.get("published"),
                "published_at": published,
                "published_time_quality": quality,
                "first_seen_at": first_seen,
                "first_seen_time_quality": first_quality,
                "last_seen_at": last_seen,
                "like_count": comment.get("like_count"),
                "reply_count": comment.get("reply_count"),
                "is_reply": bool(comment.get("is_reply")),
                "search_queries": archive.get("search_queries") or [],
            }
            add_record(records, item)
    return stats


def collect_forum(records: list[dict[str, Any]]) -> dict[str, int]:
    stats = {"threads": 0, "posts": 0}
    for path in sorted(FORUM.glob("thread_*.json")):
        archive = read_json(path, {})
        if not isinstance(archive, dict):
            continue
        thread = archive.get("thread") or {}
        title = thread.get("title") or ""
        url = thread.get("url") or ""
        tid = str(thread.get("tid") or path.stem.removeprefix("thread_"))
        stats["threads"] += 1
        pages = (archive.get("topic_api") or {}).get("pages") or []
        for page in pages:
            for post in page.get("posts") or []:
                stats["posts"] += 1
                published, quality = parse_time(post.get("created_at") or post.get("create_time"))
                pid = str(post.get("pid") or "")
                item = {
                    "record_id": f"lenovo_forum:{tid}:{pid}",
                    "platform": "lenovo_forum",
                    "record_type": "forum_post",
                    "source_title": title,
                    "source_url": url,
                    "thread_id": tid,
                    "post_id": pid,
                    "author": post.get("author"),
                    "text": post.get("message_text") or "",
                    "published_raw": post.get("created_at_text"),
                    "published_at": published,
                    "published_time_quality": quality,
                    "first_seen_at": None,
                    "first_seen_time_quality": "unknown",
                    "last_seen_at": iso(NOW),
                    "is_first_post": bool(post.get("is_first")),
                    "kudos": post.get("kudos"),
                }
                add_record(records, item)
    return stats


def sort_key(item: dict[str, Any]) -> tuple[str, str]:
    return (item.get("published_at") or "", item.get("record_id") or "")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    yt_stats = collect_youtube(records)
    forum_stats = collect_forum(records)
    records.sort(key=sort_key, reverse=True)

    dated = [r for r in records if r.get("published_at")]
    undated = [r for r in records if not r.get("published_at")]
    relevant = [r for r in records if r.get("relevance") in ("high", "medium")]
    contextual = [r for r in records if r.get("relevance") == "low"]
    exact_count = sum(r.get("published_time_quality") == "exact" for r in dated)
    estimated_count = sum(r.get("published_time_quality") == "relative_estimate" for r in dated)

    # Rolling windows are UTC, inclusive of the exact generated_at timestamp.
    cutoff_7 = NOW - timedelta(days=7)
    cutoff_30 = NOW - timedelta(days=30)
    recent_7 = [r for r in relevant if r.get("published_at") and datetime.fromisoformat(r["published_at"].replace("Z", "+00:00")) >= cutoff_7]
    recent_30 = [r for r in relevant if r.get("published_at") and datetime.fromisoformat(r["published_at"].replace("Z", "+00:00")) >= cutoff_30]
    context_7 = [r for r in contextual if r.get("published_at") and datetime.fromisoformat(r["published_at"].replace("Z", "+00:00")) >= cutoff_7]
    context_30 = [r for r in contextual if r.get("published_at") and datetime.fromisoformat(r["published_at"].replace("Z", "+00:00")) >= cutoff_30]
    recent_7_all = [r for r in records if r.get("published_at") and datetime.fromisoformat(r["published_at"].replace("Z", "+00:00")) >= cutoff_7]
    recent_30_all = [r for r in records if r.get("published_at") and datetime.fromisoformat(r["published_at"].replace("Z", "+00:00")) >= cutoff_30]

    write_json(OUT / "recent_7d.json", {
        "generated_at": iso(NOW), "window_days": 7, "date_basis": "published_at",
        "record_count": len(recent_7), "records": recent_7,
    })
    write_json(OUT / "recent_30d.json", {
        "generated_at": iso(NOW), "window_days": 30, "date_basis": "published_at",
        "record_count": len(recent_30), "records": recent_30,
    })
    write_json(OUT / "recent_7d_context.json", {
        "generated_at": iso(NOW), "window_days": 7, "relevance": "low_context",
        "record_count": len(context_7), "records": context_7,
    })
    write_json(OUT / "recent_30d_context.json", {
        "generated_at": iso(NOW), "window_days": 30, "relevance": "low_context",
        "record_count": len(context_30), "records": context_30,
    })
    write_json(OUT / "recent_7d_all.json", {
        "generated_at": iso(NOW), "window_days": 7, "includes_excluded": True,
        "record_count": len(recent_7_all), "records": recent_7_all,
    })
    write_json(OUT / "recent_30d_all.json", {
        "generated_at": iso(NOW), "window_days": 30, "includes_excluded": True,
        "record_count": len(recent_30_all), "records": recent_30_all,
    })
    write_json(OUT / "undated.json", {
        "generated_at": iso(NOW), "note": "No reliable publication timestamp; do not treat as recent.",
        "record_count": len(undated), "records": undated,
    })

    monthly: dict[str, list[dict[str, Any]]] = {}
    for record in relevant:
        if not record.get("published_at"):
            continue
        month = record["published_at"][:7]
        monthly.setdefault(month, []).append(record)
    monthly_counts = {}
    for month, items in sorted(monthly.items()):
        write_json(OUT / "monthly" / f"{month}.json", {
            "generated_at": iso(NOW), "month": month,
            "record_count": len(items), "records": items,
        })
        monthly_counts[month] = len(items)

    # One normalized full archive for broader analysis; JSONL keeps it stream-friendly.
    jsonl_path = OUT / "records.jsonl"
    temp_path = jsonl_path.with_suffix(".jsonl.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for item in records:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    temp_path.replace(jsonl_path)

    index = {
        "schema_version": 1,
        "generated_at": iso(NOW),
        "timezone": "UTC",
        "source_of_truth": ["data/comments/*.json", "data/lenovo_forum/thread_*.json"],
        "query_files": {
            "last_7_days_relevant": "data/analysis/recent_7d.json",
            "last_30_days_relevant": "data/analysis/recent_30d.json",
            "last_7_days_context": "data/analysis/recent_7d_context.json",
            "last_30_days_context": "data/analysis/recent_30d_context.json",
            "last_7_days_all": "data/analysis/recent_7d_all.json",
            "last_30_days_all": "data/analysis/recent_30d_all.json",
            "undated_or_unknown_date": "data/analysis/undated.json",
            "full_normalized_archive": "data/analysis/records.jsonl",
            "monthly_directory": "data/analysis/monthly/",
        },
        "counts": {
            "youtube_videos": yt_stats["videos"],
            "youtube_comments_and_replies": yt_stats["comments"],
            "forum_threads": forum_stats["threads"],
            "forum_posts": forum_stats["posts"],
            "all_records": len(records),
            "relevant_records": len(relevant),
            "excluded_records": len(records) - len(relevant),
            "dated_records": len(dated),
            "exact_publication_times": exact_count,
            "estimated_publication_times": estimated_count,
            "undated_records": len(undated),
            "recent_7d_relevant": len(recent_7),
            "recent_30d_relevant": len(recent_30),
            "recent_7d_context": len(context_7),
            "recent_30d_context": len(context_30),
        },
        "monthly_counts": monthly_counts,
        "relevance_guide": {
            "high": "Target 27UD-10/32UD-10 plus a direct dimming/brightness symptom.",
            "medium": "Target model plus HDR/brightness/firmware context.",
            "low": "Adjacent OLED/HDR/Lenovo context; stored in the separate context windows, not the default relevant windows.",
            "excluded": "27QD-10 or 32QD-10 when the same record does not contain Pro. 27Q/Pro27Q is retained.",
        },
        "time_quality_guide": {
            "exact": "Explicit timestamp or Unix timestamp.",
            "relative_estimate": "Estimated from a relative YouTube label and collection time; not exact.",
            "unknown": "No reliable publication timestamp. Excluded from rolling date windows.",
        },
    }
    write_json(OUT / "index.json", index)
    print(json.dumps(index, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
