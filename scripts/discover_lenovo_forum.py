#!/usr/bin/env python3
"""Discover Lenovo Community discussion URLs through its rendered Khoros search page.

This is deliberately a discovery probe: it records what the live search UI returns
before the collector starts treating any endpoint as a stable API.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

BASE = "https://forums.lenovo.com"
SEARCH_URL = BASE + "/t5/forums/searchpage/tab/message?filter=location&q="
TERMS = [
    '"Legion Pro 27UD-10" HDR',
    '"27UD-10" dimming',
    '"Legion Pro 32UD-10" HDR',
    '"32UD-10" dimming',
    '"27Q-10" brightness',
    '"HDR dimming" OLED',
    'Legion OLED brightness drops',
]
OUT = Path(__file__).resolve().parents[1] / "data" / "lenovo_forum"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "search_route": SEARCH_URL,
        "terms": [],
        "discovered_topics": {},
        "note": "Search results are collected from the rendered Lenovo Community search page; no undocumented search API is assumed.",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
        )
        page = context.new_page()
        for term in TERMS:
            url = SEARCH_URL + quote(term, safe="")
            item = {"query": term, "url": url, "topics": [], "api_responses": []}
            response_meta = []

            def on_response(response):
                if "/api/" in response.url or "search" in response.url.lower():
                    entry = {
                        "status": response.status,
                        "url": response.url[:2000],
                        "content_type": response.headers.get("content-type", ""),
                    }
                    if "json" in entry["content_type"].lower():
                        try:
                            entry["body_preview"] = response.text()[:5000]
                        except Exception:
                            pass
                    response_meta.append(entry)

            page.on("response", on_response)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                page.wait_for_timeout(2500)
                item["http_status"] = response.status if response else None
                item["final_url"] = page.url
                item["title"] = page.title()
                item["body_text"] = page.locator("body").inner_text(timeout=10000)[:18000]
                links = page.locator("a[href]").evaluate_all(
                    "(els) => els.map(a => ({href:a.href, text:(a.innerText||a.getAttribute('aria-label')||'').trim()}))"
                )
                found = {}
                for link in links:
                    match = re.search(r"/td-p/(\d+)", link.get("href", ""))
                    if match:
                        tid = match.group(1)
                        found.setdefault(tid, {
                            "tid": tid,
                            "url": link["href"].split("#")[0],
                            "link_text": link.get("text", "")[:500],
                            "query": term,
                        })
                item["topics"] = list(found.values())
                item["api_responses"] = response_meta[:40]
                for topic in item["topics"]:
                    report["discovered_topics"].setdefault(topic["tid"], topic)
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
                item["api_responses"] = response_meta[:40]
            finally:
                page.remove_listener("response", on_response)
            report["terms"].append(item)
            print(json.dumps({
                "query": term,
                "status": item.get("http_status"),
                "topics_found": len(item["topics"]),
                "final_url": item.get("final_url"),
                "error": item.get("error"),
            }, ensure_ascii=False))

        browser.close()

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["unique_topics_found"] = len(report["discovered_topics"])
    (OUT / "forum_search_discovery.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "unique_topics_found": report["unique_topics_found"],
        "topic_ids": list(report["discovered_topics"]),
        "saved": "data/lenovo_forum/forum_search_discovery.json",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
