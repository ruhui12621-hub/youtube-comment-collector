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

            # This endpoint was observed in the page's own XHR traffic:
            # /api/v2/topic/getTopic?fid=15987&tid=10042773&page=1
            # Fetch each page through the authenticated browser context so pagination is complete.
            page_records = []
            for page_number in range(1, 21):
                endpoint = f"{BASE}/api/v2/topic/getTopic?fid=15987&tid={THREAD_ID}&page={page_number}"
                response_data = page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {credentials: 'include'});
                        return {status: r.status, text: await r.text(), url: r.url};
                    }""",
                    endpoint,
                )
                if response_data["status"] != 200:
                    diagnostics.append({"method": "topic_api_page", "page": page_number,
                                        "status": response_data["status"], "url": endpoint})
                    break
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
                        normalized.append({
                            "pid": post.get("pid"),
                            "tid": post.get("tid"),
                            "subject": post.get("subject"),
                            "author": post.get("author_name") or post.get("last_author_name"),
                            "created_at": post.get("create_time"),
                            "created_at_text": post.get("last_update"),
                            "message_html": post.get("translation_message") or post.get("message") or "",
                            "message_text": clean_message(post.get("translation_message") or post.get("message") or ""),
                            "is_first": post.get("is_first"),
                            "kudos": post.get("kudos"),
                            "author_country": (post.get("authorInfo") or {}).get("country"),
                        })
                    page_records.append({
                        "page": page_number,
                        "posts_count": len(normalized),
                        "posts": normalized,
                        "posts_num": payload.get("postsNum"),
                        "cur_page": payload.get("curPage"),
                        "last_pid": payload.get("lastPid"),
                        "topic_info": payload.get("topicInfo"),
                    })
                    diagnostics.append({
                        "method": "topic_api_page", "page": page_number,
                        "status": response_data["status"], "posts_count": len(normalized),
                        "posts_num": payload.get("postsNum"), "url": response_data["url"],
                    })
                    if len(posts) == 0 or len(page_records) > 0 and len(posts) < 10:
                        # Still query next page once to verify the boundary.
                        if page_number >= 2:
                            break
                except Exception as exc:
                    diagnostics.append({"method": "topic_api_parse", "page": page_number,
                                        "error": f"{type(exc).__name__}: {exc}"})
                    break
            records["topic_api"] = {
                "endpoint_template": f"{BASE}/api/v2/topic/getTopic?fid=15987&tid={THREAD_ID}&page=N",
                "pages": page_records,
                "total_posts_captured": sum(p["posts_count"] for p in page_records),
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

    api = records.get("topic_api", {})
    all_posts = [post for page in api.get("pages", []) for post in page.get("posts", [])]
    report = {
        "started_at": started,
        "finished_at": now(),
        "thread_id": THREAD_ID,
        "http_diagnostics": http_diagnostics,
        "browser_diagnostics": browser_diagnostics,
        "browser_error": browser_error,
        "pagination": {
            "pages_captured": len(api.get("pages", [])),
            "posts_captured": len(all_posts),
            "post_ids": [p.get("pid") for p in all_posts],
            "page_counts": [p.get("posts_count") for p in api.get("pages", [])],
        },
        "note": "Topic page data is retrieved from the JSON endpoint observed in the rendered forum page. Verify pagination count against the topic's replies/posts metadata.",
    }
    (OUT / "thread_10042773.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (OUT / "forum_probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "thread_id": THREAD_ID,
        "pagination": report["pagination"],
        "browser_diagnostics": browser_diagnostics,
        "browser_error": browser_error,
        "saved": ["data/lenovo_forum/thread_10042773.json", "data/lenovo_forum/forum_probe_report.json"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
