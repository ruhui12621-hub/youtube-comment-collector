#!/usr/bin/env python3
"""Probe Lenovo Community's real search UI and Khoros REST search endpoints.

The former /t5/forums/searchpage route returns Lenovo's rendered 404 page.
This diagnostic inspects the homepage search controls and probes the classic
Khoros REST search endpoint from the same authenticated browser context.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

BASE = "https://forums.lenovo.com"
HOME = BASE + "/"
TERMS = [
    '"Legion Pro 27UD-10" HDR',
    '"27UD-10" dimming',
    '"Legion Pro 32UD-10" HDR',
    '"32UD-10" dimming',
    '"27Q-10" brightness',
    '"HDR dimming" OLED',
    'Legion OLED brightness drops',
]
REST_CANDIDATES = [
    "/restapi/vc/search/messages?q={q}&restapi.response_style=view",
    "/restapi/vc/search/messages?q={q}&restapi.response_style=view&restapi.format_detail=full_list_element",
    "/api/v2/search/messages?q={q}",
    "/api/v2/search?query={q}",
]
OUT = Path(__file__).resolve().parents[1] / "data" / "lenovo_forum"


def extract_topics(body: str, term: str) -> list[dict]:
    found = {}
    for match in re.finditer(r'(?:"view_href"|"href"|"url"|view_href|href|url)\s*[:=]\s*["\']([^"\']+)["\']', body, re.I):
        url = match.group(1)
        tid_match = re.search(r"/td-p/(\d+)", url)
        if tid_match:
            tid = tid_match.group(1)
            found.setdefault(tid, {
                "tid": tid,
                "url": url if url.startswith("http") else BASE + url,
                "query": term,
                "source": "rest_search",
            })
    try:
        obj = json.loads(body)
        stack = [obj]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                url = str(value.get("view_href") or value.get("url") or value.get("href") or "")
                tid = str(value.get("topic_id") or value.get("thread_id") or value.get("tid") or "")
                match = re.search(r"/td-p/(\d+)", url)
                tid = match.group(1) if match else tid
                if tid.isdigit():
                    found.setdefault(tid, {
                        "tid": tid,
                        "url": url if url.startswith("http") else (BASE + url if url.startswith("/") else ""),
                        "query": term,
                        "source": "rest_search_json",
                    })
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    except Exception:
        pass
    return list(found.values())


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "homepage": HOME,
        "terms": [],
        "discovered_topics": {},
        "diagnosis": {},
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
        )
        page = context.new_page()
        response = page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(4000)

        report["diagnosis"]["homepage"] = {
            "http_status": response.status if response else None,
            "final_url": page.url,
            "title": page.title(),
            "body_text": page.locator("body").inner_text(timeout=10000)[:10000],
            "forms": page.locator("form").evaluate_all(
                "(els) => els.map(f => ({action:f.action,method:f.method,html:f.outerHTML.slice(0,5000)}))"
            ),
            "inputs": page.locator("input").evaluate_all(
                "(els) => els.map(e => ({type:e.type,name:e.name,id:e.id,placeholder:e.placeholder,aria:e.getAttribute('aria-label'),value:e.value,outer:e.outerHTML.slice(0,1000)}))"
            ),
            "buttons": page.locator("button").evaluate_all(
                "(els) => els.map(e => ({text:(e.innerText||'').trim(),aria:e.getAttribute('aria-label'),title:e.title,outer:e.outerHTML.slice(0,1000)}))"
            ),
            "search_links": page.locator("a[href*='search']").evaluate_all(
                "(els) => els.map(e => ({href:e.href,text:(e.innerText||'').trim()})).slice(0,50)"
            ),
        }

        for term in TERMS:
            item = {"query": term, "rest_probes": [], "topics": []}
            encoded = quote(term, safe="")
            for template in REST_CANDIDATES:
                url = BASE + template.format(q=encoded)
                try:
                    result = page.evaluate("""async (url) => {
                        const r = await fetch(url, {credentials:'include', headers:{'Accept':'application/json, application/xml, text/xml, */*'}});
                        const body = await r.text();
                        return {status:r.status, url:r.url, content_type:r.headers.get('content-type') || '', body:body.slice(0,16000)};
                    }""", url)
                    probe = {
                        "status": result["status"],
                        "url": result["url"],
                        "content_type": result["content_type"],
                        "body_preview": result["body"][:12000],
                    }
                    if result["status"] == 200 and "Page Not Found" not in result["body"] and "Sorry, the page you were looking for" not in result["body"]:
                        topics = extract_topics(result["body"], term)
                        probe["topics_found"] = len(topics)
                        item["topics"].extend(topics)
                        probe["looks_like_search_results"] = (
                            "restapi" in result["body"].lower()
                            or "message" in result["body"].lower()
                            or "application/json" in result["content_type"].lower()
                        )
                    item["rest_probes"].append(probe)
                except Exception as exc:
                    item["rest_probes"].append({"url": url, "error": f"{type(exc).__name__}: {exc}"})

            unique = {}
            for topic in item["topics"]:
                unique.setdefault(topic["tid"], topic)
            item["topics"] = list(unique.values())
            for topic in item["topics"]:
                report["discovered_topics"].setdefault(topic["tid"], topic)
            report["terms"].append(item)
            print(json.dumps({
                "query": term,
                "rest_statuses": [probe.get("status") for probe in item["rest_probes"]],
                "topics_found": len(item["topics"]),
            }, ensure_ascii=False))

        browser.close()

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["unique_topics_found"] = len(report["discovered_topics"])
    report["search_status"] = "success" if report["unique_topics_found"] else "needs_investigation"
    report["diagnosis"]["note"] = (
        "A search is counted as successful only when actual topic IDs are extracted. "
        "HTTP 200 alone is not sufficient. Inspect homepage controls and REST response previews."
    )
    (OUT / "forum_search_discovery.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "search_status": report["search_status"],
        "unique_topics_found": report["unique_topics_found"],
        "topic_ids": list(report["discovered_topics"]),
        "saved": "data/lenovo_forum/forum_search_discovery.json",
    }, ensure_ascii=False, indent=2))
    if not report["unique_topics_found"]:
        print("::warning::Lenovo forum search did not return extractable topic IDs; see diagnostic report.")


if __name__ == "__main__":
    main()
