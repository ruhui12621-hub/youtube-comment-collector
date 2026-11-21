#!/usr/bin/env python3
"""Probe and archive public Lenovo forum discussions (Khoros/Lithium + HTML fallback)."""
from __future__ import annotations
import json, re, time
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
    'Lenovo OLED HDR brightness drops',
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
    "Accept": "application/json, text/html, */*",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

def now():
    return datetime.now(timezone.utc).isoformat()

def get(url, params=None):
    try:
        r = SESSION.get(url, params=params, timeout=35)
        return {"status": r.status_code, "url": r.url, "content_type": r.headers.get("content-type",""),
                "text": r.text[:5_000_000]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "url": url}

def parse_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for x in soup(["script", "style", "noscript"]):
        x.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return {
        "title": (soup.title.get_text(" ", strip=True) if soup.title else None),
        "description": (soup.find("meta", attrs={"name":"description"}) or {}).get("content"),
        "text": text[:100000],
        "links": [{"text":a.get_text(" ",strip=True)[:180], "href":a.get("href")}
                  for a in soup.find_all("a", href=True)
                  if any(k in (a.get("href") or "").lower() for k in ("thread","message","reply","td-p/"))][:300],
    }

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    started = now()
    diagnostics = []
    records = {}
    # Khoros REST v2 endpoints. Try both legacy and current response formats.
    endpoint_attempts = [
        (f"{BASE}/api/2.0/messages/{THREAD_ID}", {"restapi.response_format":"json"}),
        (f"{BASE}/api/2.0/messages/{THREAD_ID}/replies", {"restapi.response_format":"json", "page_size":"100"}),
        (f"{BASE}/api/2.0/search", {"q":f'id:{THREAD_ID}', "restapi.response_format":"json"}),
        (f"{BASE}/api/2.0/search", {"q":'"HDR Dimming Issue on Lenovo Legion Pro"', "restapi.response_format":"json"}),
    ]
    for url, params in endpoint_attempts:
        result = get(url, params)
        entry = {k:v for k,v in result.items() if k != "text"}
        entry["sample"] = result.get("text","")[:1200]
        diagnostics.append(entry)
        if result.get("status") == 200 and "json" in result.get("content_type","").lower():
            try:
                payload = json.loads(result["text"])
                records["api_" + str(len(records)+1)] = payload
            except Exception:
                pass
        time.sleep(.5)

    # Direct thread URL variants: capture server-rendered HTML and embedded JSON.
    thread_urls = [
        f"{BASE}/t5/Displays-Options-and-Accessories/HDR-Dimming-Issue-on-Lenovo-Legion-Pro/td-p/{THREAD_ID}",
        f"{BASE}/t5/Monitors-and-Displays/HDR-Dimming-Issue-on-Lenovo-Legion-Pro/td-p/{THREAD_ID}",
        f"{BASE}/t5/Lenovo-Forums/HDR-Dimming-Issue-on-Lenovo-Legion-Pro/td-p/{THREAD_ID}",
    ]
    for i, url in enumerate(thread_urls, 1):
        result = get(url)
        diagnostics.append({k:v for k,v in result.items() if k != "text"})
        if result.get("status") == 200 and "text" in result:
            parsed = parse_html(result["text"])
            parsed["source_url"] = result["url"]
            records[f"thread_html_{i}"] = parsed
            # Preserve embedded state blobs that may contain message bodies.
            soup = BeautifulSoup(result["text"], "html.parser")
            embedded = []
            for tag in soup.find_all("script"):
                raw = tag.string or tag.get_text()
                if raw and any(term in raw.lower() for term in ("messagebody","message.body","replycount","10042773")):
                    embedded.append(raw[:250000])
            if embedded:
                records[f"thread_embedded_{i}"] = embedded
        time.sleep(.5)

    search_pages = []
    for term in SEARCH_TERMS:
        # Khoros search API, followed by public HTML search fallback.
        api = get(f"{BASE}/api/2.0/search", {"q":term, "restapi.response_format":"json", "page_size":"100"})
        item = {k:v for k,v in api.items() if k != "text"}
        item["query"] = term
        if api.get("status") == 200 and "text" in api:
            try:
                item["json"] = json.loads(api["text"])
            except Exception:
                item["sample"] = api["text"][:3000]
        search_pages.append(item)
        time.sleep(.6)

    report = {
        "started_at": started, "finished_at": now(),
        "thread_id": THREAD_ID,
        "endpoint_diagnostics": diagnostics,
        "search_attempts": search_pages,
        "record_keys": list(records.keys()),
        "note": "A 200 response is not proof of complete thread retrieval; inspect payload shape and pagination."
    }
    (OUT / "thread_10042773.json").write_text(json.dumps(records, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (OUT / "forum_probe_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({
        "thread_id": THREAD_ID,
        "record_keys": list(records.keys()),
        "endpoint_results": diagnostics,
        "search_results": [{k:v for k,v in x.items() if k not in ("json","sample")} for x in search_pages],
        "saved": ["data/lenovo_forum/thread_10042773.json","data/lenovo_forum/forum_probe_report.json"]
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
