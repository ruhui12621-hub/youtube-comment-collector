#!/usr/bin/env python3
"""Discover relevant YouTube videos and archive their public comments/replies."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ytscrape import YouTube
from search_rules import load_search_queries, is_relevant_monitor_text

ROOT = Path(__file__).resolve().parents[1]
MANUAL_FILE = ROOT / "video_urls.txt"
COMMENTS_DIR = ROOT / "data" / "comments"
INDEX_FILE = ROOT / "data" / "videos.json"
REPORT_FILE = ROOT / "data" / "collection_report.json"

VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)?([A-Za-z0-9_-]{11})")
MAX_VIDEOS_PER_QUERY = max(1, int(os.getenv("MAX_VIDEOS_PER_QUERY", "6")))
PAUSE_BETWEEN_VIDEOS = 1.5


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def extract_video_id(value: str) -> str | None:
    value = value.strip()
    match = VIDEO_ID_RE.search(value)
    return match.group(1) if match else None


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def comment_to_dict(comment: Any) -> dict[str, Any]:
    fields = (
        "comment_id",
        "text",
        "author",
        "author_channel_id",
        "published",
        "published_at",
        "parent_id",
        "like_count",
        "like_count_text",
        "reply_count",
        "reply_count_text",
        "heart",
        "is_reply",
    )
    result = {name: getattr(comment, name, None) for name in fields}
    for name, value in result.items():
        if isinstance(value, datetime):
            result[name] = value.astimezone(timezone.utc).isoformat()
    result["comment_id"] = str(result["comment_id"])
    return result


def main() -> int:
    queries = load_search_queries()
    manual_entries = read_lines(MANUAL_FILE)
    discovered: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    started_at = utc_now()

    print(f"Loaded {len(queries)} search queries.")
    print(f"Manual video entries: {len(manual_entries)}")

    with YouTube() as yt:
        for query in queries:
            print(f"\n[SEARCH] {query}")
            try:
                results = yt.search(query, filter="videos", max_results=MAX_VIDEOS_PER_QUERY)
                count = 0
                for video in results:
                    video_id = getattr(video, "video_id", None)
                    if not video_id:
                        continue
                    title = getattr(video, "title", None)
                    channel = getattr(video, "channel", None)
                    if not is_relevant_monitor_text(title):
                        print(f"  [EXCLUDED unrelated monitor result] {title}")
                        continue
                    item = discovered.setdefault(
                        video_id,
                        {
                            "video_id": video_id,
                            "url": getattr(video, "url", None)
                            or f"https://www.youtube.com/watch?v={video_id}",
                            "title": getattr(video, "title", None),
                            "channel": getattr(video, "channel", None),
                            "search_queries": [],
                        },
                    )
                    if query not in item["search_queries"]:
                        item["search_queries"].append(query)
                    count += 1
                print(f"  Found {count} video results.")
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                print(f"  Search failed: {message}", file=sys.stderr)
                errors.append({"stage": "search", "target": query, "error": message})

        for entry in manual_entries:
            video_id = extract_video_id(entry)
            if not video_id:
                errors.append(
                    {"stage": "manual_input", "target": entry, "error": "Could not parse video ID"}
                )
                continue
            discovered.setdefault(
                video_id,
                {
                    "video_id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "title": None,
                    "channel": None,
                    "search_queries": ["manual"],
                },
            )

        previous_index = read_json(INDEX_FILE, {"videos": []})
        previous_by_id = {
            item.get("video_id"): item
            for item in previous_index.get("videos", [])
            if item.get("video_id")
        }

        # Keep previously discovered videos in the index even if they no longer
        # appear in the current YouTube search results.
        merged_index = dict(previous_by_id)
        merged_index.update(discovered)

        collected_videos = 0
        total_comments_seen = 0
        successful_videos = 0

        for video_id, video in discovered.items():
            COMMENTS_DIR.mkdir(parents=True, exist_ok=True)
            output_path = COMMENTS_DIR / f"{video_id}.json"
            existing = read_json(output_path, {})
            old_comments = {
                str(item.get("comment_id")): item
                for item in existing.get("comments", [])
                if item.get("comment_id") is not None
            }

            print(f"\n[COMMENTS] {video.get('title') or video_id}")
            print(f"  {video.get('url')}")
            new_comments: dict[str, dict[str, Any]] = {}
            try:
                # No max_results cap: consume the lazy iterator through all
                # available pages, including replies, in newest-first order.
                thread = yt.comments(
                    video_id,
                    include_replies=True,
                    sort="newest",
                )
                for comment in thread:
                    item = comment_to_dict(comment)
                    previous = old_comments.get(item["comment_id"])
                    # Preserve known first-seen metadata. For legacy comments,
                    # do not pretend today's backfill is their original first-seen date.
                    if previous and previous.get("first_seen_at"):
                        item["first_seen_at"] = previous.get("first_seen_at")
                        item["first_seen_time_quality"] = previous.get(
                            "first_seen_time_quality", "exact"
                        )
                    elif previous:
                        item["first_seen_at"] = None
                        item["first_seen_time_quality"] = "unknown_legacy"
                    else:
                        item["first_seen_at"] = started_at
                        item["first_seen_time_quality"] = "exact"
                    item["last_seen_at"] = started_at
                    new_comments[item["comment_id"]] = item

                # Merge by YouTube's stable comment ID so deleted/temporarily
                # unavailable comments from older runs remain in the archive.
                merged_comments = dict(old_comments)
                merged_comments.update(new_comments)
                ordered_comments = list(merged_comments.values())

                record = {
                    "video_id": video_id,
                    "url": video.get("url"),
                    "title": video.get("title") or existing.get("title"),
                    "channel": video.get("channel") or existing.get("channel"),
                    "search_queries": video.get("search_queries", []),
                    "first_collected_at": existing.get("first_collected_at", started_at),
                    "last_collected_at": utc_now(),
                    "comments_count": len(ordered_comments),
                    "comments_in_latest_fetch": len(new_comments),
                    "comments": ordered_comments,
                }
                write_json(output_path, record)
                collected_videos += 1
                total_comments_seen += len(new_comments)
                successful_videos += 1
                print(
                    f"  Retrieved {len(new_comments)} comments/replies; "
                    f"{len(ordered_comments)} in archive."
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                print(f"  Comment collection failed: {message}", file=sys.stderr)
                errors.append({"stage": "comments", "target": video_id, "error": message})

            time.sleep(PAUSE_BETWEEN_VIDEOS)

    index_items = sorted(
        merged_index.values(),
        key=lambda item: (item.get("title") or item.get("video_id") or "").casefold(),
    )
    write_json(
        INDEX_FILE,
        {
            "updated_at": utc_now(),
            "video_count": len(index_items),
            "videos": index_items,
        },
    )

    report = {
        "started_at": started_at,
        "finished_at": utc_now(),
        "queries": queries,
        "videos_discovered_this_run": len(discovered),
        "videos_collected_successfully": successful_videos,
        "comments_and_replies_seen_this_run": total_comments_seen,
        "errors": errors,
    }
    write_json(REPORT_FILE, report)

    print("\n=== Collection summary ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Partial failures are recorded in the report but do not discard successful data.
    return 0 if successful_videos or not discovered else 1


if __name__ == "__main__":
    raise SystemExit(main())
