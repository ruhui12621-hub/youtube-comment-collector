#!/usr/bin/env python3
"""Collect Lenovo Community thread content using the rendered page and its observed JSON API."""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "lenovo_forum"
THREAD_ID = "10042773"
THREAD_FID = "15987"
BASE = "https://forums.lenovo.com"
THREAD_URL = f"{BASE}/t5/Displays-Options-and-Accessories/HDR-Dimming-Issue-on-Lenovo-Legion-Pro/td-p/{THREAD_ID}"
SEARCH_TERMS = [
    '"Legion Pro 27UD-10" HDR',
    '"Legion Pro 32UD-10" HDR',
    '"27UD-10" dimming',
    '"32UD-10" dimming',
    '"27Q-10" brightness',
    '"HDR Dimming Issue on Lenovo Legion Pro"',
    "Lenovo OLED HDR brightness drops",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
    "Accept": "text/html,application/json,*/*",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_html(html: str, source_url: str):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    description = (soup.find("meta", attrs={"name": "description"}) or {}).get("content")
    scripts = []
    for tag in soup.find_all("script"):
        raw = tag.string or tag.get_text()
        if raw and any(k in raw.lower() for k in ("message", "thread", "reply", THREAD_ID, "lithium")):
            scripts.append(raw[:300_000])
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    body = soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", body.get_text("\n", strip=True))
    return {
        "title": title,
        "description": description,
        "text": text[:250_000],
        "source_url": source_url,
        "embedded_scripts": scripts[:50],
    }


def clean_message(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return re.sub(r"\n{3,}", "\n\n", unescape(soup.get_text("\n", strip=True))).strip()


def http_probe():
    diagnostics, records = [], {}
    for url in (THREAD_URL,):
        try:
            response = SESSION.get(url, timeout=35)
            diagnostics.append({
                "method": "requests", "status": response.status_code,
                "url": response.url, "content_type": response.headers.get("content-type", ""),
            })
            if response.ok and len(response.text) > 1500:
                records["http_thread"] = parse_html(response.text, response.url)
        except Exception as exc:
            diagnostics.append({"method": "requests", "url": url, "error": f"{type(exc).__name__}: {exc}"})

    for term in SEARCH_TERMS:
        url = f"{BASE}/api/v2/search?{urlencode({'q': term})}"
        try:
            response = SESSION.get(url, timeout=20)
            diagnostics.append({
                "method": "search_api_probe", "query": term, "status": response.status_code,
                "url": response.url, "content_type": response.headers.get("content-type", ""),
            })
        except Exception as exc:
            diagnostics.append({"method": "search_api_probe", "query": term, "error": f"{type(exc).__name__}: {exc}"})
    return diagnostics, records


def load_target_threads():
    """Load relevant model-specific topics discovered by the previous workflow run."""
    targets = {
        THREAD_ID: {
            "tid": THREAD_ID, "fid": THREAD_FID, "url": THREAD_URL,
            "title": "HDR Dimming Issue on Lenovo Legion Pro, Can't Update Firmware 27Q-10",
            "source": "pinned_thread",
        }
    }
    discovery_path = OUT / "forum_search_discovery.json"
    if not discovery_path.exists():
        return list(targets.values())

    try:
        report = json.loads(discovery_path.read_text(encoding="utf-8"))
    except Exception:
        return list(targets.values())

    model_pattern = re.compile(r"(?:27\s?u?d|32\s?u?d|27q[d]?|32q[d]?)-10", re.I)
    issue_pattern = re.compile(r"(hdr|dimm|bright|abl|low power|oled care)", re.I)
    for tid, item in (report.get("discovered_topics") or {}).items():
        url = item.get("url") or ""
        match = re.search(r"/topic/findpost/(\d+)/(\d+)/(\d+)", url)
        if not match:
            continue
        fid, parsed_tid, pid = match.groups()
        title = item.get("title") or ""
        query = item.get("query") or ""
        # Require the actual result card title/snippet to mention both the model and
        # a brightness/HDR-related symptom; do not let a broad search query pull in
        # unrelated topics that happened to appear in the result list.
        if not (model_pattern.search(title) and issue_pattern.search(title)):
            continue
        if parsed_tid not in targets:
            targets[parsed_tid] = {
                "tid": parsed_tid, "fid": fid, "url": url,
                "title": title[:500], "query": query,
                "source": "search_discovery", "sample_pid": pid,
            }
    return list(targets.values())


def collect_topic_pages(page, thread, max_pages=20):
    """Fetch all available topic pages through the authenticated browser context."""
    pages = []
    fid, tid = thread["fid"], thread["tid"]
    for page_number in range(1, max_pages + 1):
        endpoint = f"{BASE}/api/v2/topic/getTopic?fid={fid}&tid={tid}&page={page_number}"
        response_data = page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: 'include'});
                return {status: r.status, text: await r.text(), url: r.url};
            }""",
            endpoint,
        )
        if response_data["status"] != 200:
            return pages, {"page": page_number, "status": response_data["status"], "url": endpoint}
        try:
            envelope = json.loads(response_data["text"])
            encoded = envelope.get("data")
            if not encoded:
                break
            payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
            posts = payload.get("postsInfo") or []
            if not posts:
                break
            normalized = []
            for post in posts:
                message = post.get("translation_message") or post.get("message") or ""
                normalized.append({
                    "pid": post.get("pid"),
                    "tid": post.get("tid"),
                    "subject": post.get("subject"),
                    "author": post.get("author_name") or post.get("last_author_name"),
                    "created_at": post.get("create_time"),
                    "created_at_text": post.get("last_update"),
                    "message_html": message,
                    "message_text": clean_message(message),
                    "is_first": post.get("is_first"),
                    "kudos": post.get("kudos"),
                    "author_country": (post.get("authorInfo") or {}).get("country"),
                })
            pages.append({
                "page": page_number, "posts_count": len(normalized), "posts": normalized,
                "posts_num": payload.get("postsNum"), "cur_page": payload.get("curPage"),
                "last_pid": payload.get("lastPid"), "topic_info": payload.get("topicInfo"),
            })
            # A short page is the last page; page 1 is also checked this way.
            if len(posts) < 10:
                break
            time.sleep(0.25)
        except Exception as exc:
            return pages, {"page": page_number, "error": f"{type(exc).__name__}: {exc}"}
    return pages, None


def browser_probe():
    """Load the forum normally, then call its observed topic JSON endpoint for every page."""
    from playwright.sync_api import sync_playwright

    diagnostics, records = [], {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
        )
        page = context.new_page()
        captured = []

        def on_response(response):
            req = response.request
            if req.resource_type not in ("xhr", "fetch", "document"):
                return
            item = {
                "status": response.status,
                "url": response.url[:2000],
                "resource_type": req.resource_type,
                "content_type": response.headers.get("content-type", ""),
            }
            if "json" in item["content_type"].lower() or "/api/v2/topic/getTopic" in response.url:
                try:
                    body = response.text()
                    item["body"] = body[:300_000]
                except Exception as exc:
                    item["body_error"] = str(exc)[:300]
            captured.append(item)

        page.on("response", on_response)
        try:
            response = page.goto(THREAD_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(5000)
            html = page.content()
            body_text = page.locator("body").inner_text(timeout=10000)
            records["browser_thread"] = parse_html(html, page.url)
            records["browser_thread"]["rendered_text"] = body_text[:300_000]
            records["browser_thread"]["document_length"] = len(html)
            records["browser_thread"]["http_status"] = response.status if response else None
            diagnostics.append({
                "method": "playwright", "status": response.status if response else None,
                "final_url": page.url, "title": page.title(),
                "document_length": len(html), "body_text_length": len(body_text),
            })

            # Collect the pinned thread plus relevant model-specific topics found previously.
            target_threads = load_target_threads()
            topic_collections = {}
            for thread in target_threads:
                pages, error = collect_topic_pages(page, thread)
                total = sum(item["posts_count"] for item in pages)
                topic_collections[thread["tid"]] = {
                    "thread": thread, "pages": pages,
                    "total_posts_captured": total, "error": error,
                }
                diagnostics.append({
                    "method": "topic_api_collection", "tid": thread["tid"],
                    "fid": thread["fid"], "title": thread.get("title", ""),
                    "pages_captured": len(pages), "posts_captured": total,
                    "error": error,
                })
            records["topic_collections"] = topic_collections
            pinned = topic_collections.get(THREAD_ID, {})
            records["topic_api"] = {
                "endpoint_template": f"{BASE}/api/v2/topic/getTopic?fid={THREAD_FID}&tid={THREAD_ID}&page=N",
                "pages": pinned.get("pages", []),
                "total_posts_captured": pinned.get("total_posts_captured", 0),
            }
        except Exception as exc:
            diagnostics.append({"method": "playwright", "url": THREAD_URL,
                                "error": f"{type(exc).__name__}: {exc}"})

        interesting = []
        for item in captured:
            if item.get("body"):
                item["body_length"] = len(item["body"])
            interesting.append(item)
        records["browser_network_responses"] = interesting[:100]
        browser.close()
    return diagnostics, records


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    started = now()
    http_diagnostics, records = http_probe()
    browser_diagnostics, browser_error = [], None
    try:
        browser_diagnostics, browser_records = browser_probe()
        records.update(browser_records)
    except Exception as exc:
        browser_error = f"{type(exc).__name__}: {exc}"

    collections = records.get("topic_collections", {})
    pinned = collections.get(THREAD_ID, {})
    pinned_pages = pinned.get("pages", [])
    pinned_posts = [post for page in pinned_pages for post in page.get("posts", [])]
    topic_summary = {}
    for tid, collection in collections.items():
        posts = [post for page in collection.get("pages", []) for post in page.get("posts", [])]
        topic_summary[tid] = {
            "title": (collection.get("thread") or {}).get("title"),
            "fid": (collection.get("thread") or {}).get("fid"),
            "pages_captured": len(collection.get("pages", [])),
            "posts_captured": len(posts),
            "post_ids": [post.get("pid") for post in posts],
            "error": collection.get("error"),
        }
    report = {
        "started_at": started,
        "finished_at": now(),
        "thread_id": THREAD_ID,
        "http_diagnostics": http_diagnostics,
        "browser_diagnostics": browser_diagnostics,
        "browser_error": browser_error,
        "pagination": {
            "pages_captured": len(pinned_pages),
            "posts_captured": len(pinned_posts),
            "post_ids": [p.get("pid") for p in pinned_posts],
            "page_counts": [p.get("posts_count") for p in pinned_pages],
        },
        "topic_collections": topic_summary,
        "topics_attempted": len(topic_summary),
        "topics_with_posts": sum(1 for x in topic_summary.values() if x["posts_captured"] > 0),
        "total_posts_captured": sum(x["posts_captured"] for x in topic_summary.values()),
        "note": "Topic pages are fetched through the authenticated browser context using the observed Lenovo JSON endpoint. Discovered topics are read from the previous run's forum_search_discovery.json.",
    }
    # Preserve the established pinned-thread file and create one file per discovered topic.
    for tid, collection in collections.items():
        thread = collection.get("thread") or {}
        topic_file = {
            "thread": thread,
            "topic_api": {
                "endpoint_template": f"{BASE}/api/v2/topic/getTopic?fid={thread.get('fid')}&tid={tid}&page=N",
                "pages": collection.get("pages", []),
                "total_posts_captured": collection.get("total_posts_captured", 0),
                "error": collection.get("error"),
            },
        }
        (OUT / f"thread_{tid}.json").write_text(
            json.dumps(topic_file, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (OUT / "forum_probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "thread_id": THREAD_ID,
        "pagination": report["pagination"],
        "topics_attempted": report["topics_attempted"],
        "topics_with_posts": report["topics_with_posts"],
        "total_posts_captured": report["total_posts_captured"],
        "browser_diagnostics": browser_diagnostics,
        "browser_error": browser_error,
        "saved_directory": "data/lenovo_forum/",
        "thread_files_written": len(collections),
        "report_file": "data/lenovo_forum/forum_probe_report.json",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
