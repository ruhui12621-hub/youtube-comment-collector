#!/usr/bin/env python3
"""Collect public Lenovo Community thread content using HTTP and a headless browser fallback."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "lenovo_forum"
THREAD_ID = "10042773"
BASE = "https://forums.lenovo.com"
SEARCH_TERMS = [
    '"Legion Pro 27UD-10" HDR',
    '"Legion Pro 32UD-10" HDR',
    '"27UD-10" dimming',
    '"32UD-10" dimming',
    '"27Q-10" brightness',
    '"HDR Dimming Issue on Lenovo Legion Pro"',
    "Lenovo OLED HDR brightness drops",
]
THREAD_URLS = [
    f"{BASE}/t5/Displays-Options-and-Accessories/HDR-Dimming-Issue-on-Lenovo-Legion-Pro/td-p/{THREAD_ID}",
    f"{BASE}/t5/Monitors-and-Displays/HDR-Dimming-Issue-on-Lenovo-Legion-Pro/td-p/{THREAD_ID}",
    f"{BASE}/t5/Lenovo-Forums/HDR-Dimming-Issue-on-Lenovo-Legion-Pro/td-p/{THREAD_ID}",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
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
    # Keep scripts separately before removing them: forum state is often hydrated from JSON.
    scripts = []
    for tag in soup.find_all("script"):
        raw = tag.string or tag.get_text()
        if raw and any(k in raw.lower() for k in ("message", "thread", "reply", "10042773", "lithium")):
            scripts.append(raw[:500_000])
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    body = soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", body.get_text("\n", strip=True))
    links = [
        {"text": a.get_text(" ", strip=True)[:180], "href": a.get("href")}
        for a in soup.find_all("a", href=True)
        if any(k in (a.get("href") or "").lower() for k in ("thread", "message", "reply", "td-p/"))
    ][:500]
    return {
        "title": title,
        "description": description,
        "text": text[:250_000],
        "links": links,
        "source_url": source_url,
        "embedded_scripts": scripts[:100],
    }


def http_probe():
    diagnostics, records = [], {}
    for i, url in enumerate(THREAD_URLS, 1):
        try:
            response = SESSION.get(url, timeout=35)
            diagnostics.append({
                "method": "requests", "status": response.status_code,
                "url": response.url, "content_type": response.headers.get("content-type", ""),
            })
            if response.ok and len(response.text) > 1500:
                records[f"http_thread_{i}"] = parse_html(response.text, response.url)
        except Exception as exc:
            diagnostics.append({"method": "requests", "url": url, "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(0.3)

    # Search endpoints are retained as diagnostics only; this forum's current deployment
    # returned a branded 404 for the guessed /api/2.0/search route.
    for term in SEARCH_TERMS:
        url = f"{BASE}/api/2.0/search?{urlencode({'q': term, 'restapi.response_format': 'json', 'page_size': 100})}"
        try:
            response = SESSION.get(url, timeout=25)
            diagnostics.append({
                "method": "search_api_probe", "query": term, "status": response.status_code,
                "url": response.url, "content_type": response.headers.get("content-type", ""),
                "sample": response.text[:500] if response.status_code != 200 else None,
            })
        except Exception as exc:
            diagnostics.append({"method": "search_api_probe", "query": term, "error": f"{type(exc).__name__}: {exc}"})
    return diagnostics, records


def browser_probe():
    """Use Chromium to execute the forum's JavaScript and retain XHR/fetch payloads."""
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
            request = response.request
            if request.resource_type not in ("xhr", "fetch", "document"):
                return
            url = response.url
            item = {
                "status": response.status,
                "url": url[:2000],
                "resource_type": request.resource_type,
                "content_type": response.headers.get("content-type", ""),
            }
            if "json" in item["content_type"].lower() or any(x in url.lower() for x in ("api", "restapi", "message", "thread")):
                try:
                    body = response.text()
                    item["body"] = body[:1_000_000]
                except Exception as exc:
                    item["body_error"] = str(exc)[:300]
            captured.append(item)

        page.on("response", on_response)
        for i, url in enumerate(THREAD_URLS, 1):
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(7000)
                # Scroll once to trigger lazy-loaded replies and pagination widgets.
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2500)
                html = page.content()
                body_text = page.locator("body").inner_text(timeout=10000)
                parsed = parse_html(html, page.url)
                parsed["rendered_text"] = body_text[:300_000]
                parsed["document_length"] = len(html)
                parsed["http_status"] = response.status if response else None
                records[f"browser_thread_{i}"] = parsed
                diagnostics.append({
                    "method": "playwright", "status": response.status if response else None,
                    "requested_url": url, "final_url": page.url,
                    "title": awaitable_title(page), "document_length": len(html),
                    "body_text_length": len(body_text),
                })
            except Exception as exc:
                diagnostics.append({"method": "playwright", "url": url, "error": f"{type(exc).__name__}: {exc}"})
            # One thread ID is enough; further paths often resolve to the same page.
            if records.get(f"browser_thread_{i}", {}).get("document_length", 0) > 5000:
                break
        browser.close()

    # Preserve response bodies separately for inspecting the actual Khoros data endpoints.
    interesting = []
    for item in captured:
        body = item.pop("body", None)
        if body is not None:
            item["body_length"] = len(body)
            interesting.append(item | {"body": body})
        elif item["resource_type"] in ("xhr", "fetch"):
            interesting.append(item)
    records["browser_network_responses"] = interesting[:80]
    return diagnostics, records


def awaitable_title(page):
    try:
        return page.title()
    except Exception:
        return None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    started = now()
    diagnostics, records = http_probe()
    browser_diagnostics = []
    browser_error = None
    try:
        browser_diagnostics, browser_records = browser_probe()
        records.update(browser_records)
    except Exception as exc:
        browser_error = f"{type(exc).__name__}: {exc}"

    # Determine whether we got actual discussion content rather than the generic shell.
    text_candidates = []
    for key, value in records.items():
        if isinstance(value, dict):
            text = value.get("rendered_text") or value.get("text") or ""
            if text:
                text_candidates.append({"record": key, "text_length": len(text), "preview": text[:1000]})
    report = {
        "started_at": started,
        "finished_at": now(),
        "thread_id": THREAD_ID,
        "http_diagnostics": diagnostics,
        "browser_diagnostics": browser_diagnostics,
        "browser_error": browser_error,
        "content_checks": text_candidates,
        "record_keys": list(records.keys()),
        "note": "A generic forum shell or HTTP 200 is not considered a successful thread capture. Inspect rendered text and captured XHR/fetch payloads for message bodies and pagination.",
    }
    (OUT / "thread_10042773.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (OUT / "forum_probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "thread_id": THREAD_ID,
        "record_keys": list(records.keys()),
        "browser_diagnostics": browser_diagnostics,
        "browser_error": browser_error,
        "content_checks": text_candidates,
        "saved": [
            "data/lenovo_forum/thread_10042773.json",
            "data/lenovo_forum/forum_probe_report.json",
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
