#!/usr/bin/env python3
"""Diagnose and discover Lenovo forum topics through the rendered global-search UI."""
import json, re
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
from search_rules import load_search_queries, should_exclude_text

BASE = "https://forums.lenovo.com"
HOME = BASE + "/"
TERMS = load_search_queries()
OUT = Path(__file__).resolve().parents[1] / "data" / "lenovo_forum"

def topic_links(page):
    # Lenovo's current search results may use message links rather than td-p links.
    return page.locator('a[href*="/t5/"], a[href*="/td-p/"], a[href*="/m-p/"]').evaluate_all(
        """(els) => els.map(a => ({
            url: a.href,
            text: (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 500),
            parent: (a.closest('article, li, tr, [class*=result], [class*=topic]')?.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 1200)
        })).filter(x => x.text || x.parent).slice(0, 300)"""
    )

def inspect_search_dom(page):
    return page.evaluate("""() => {
      const nodes = [...document.querySelectorAll('#searchInput')];
      return {
        inputs: nodes.map(el => {
          const chain = [];
          let n = el;
          for (let i=0; n && i<8; i++, n=n.parentElement) {
            chain.push({tag:n.tagName,id:n.id,cls:String(n.className||'').slice(0,240),
              inert:n.hasAttribute('inert'),ariaHidden:n.getAttribute('aria-hidden'),
              display:getComputedStyle(n).display,visibility:getComputedStyle(n).visibility,
              html:n.outerHTML.slice(0,900)});
          }
          return {html:el.outerHTML,rect:(()=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})(),
            disabled:el.disabled,readOnly:el.readOnly,chain};
        }),
        searchButtons:[...document.querySelectorAll('button[aria-label="Search"], [role="button"][aria-label="Search"]')]
          .map(el=>({html:el.outerHTML.slice(0,1200),rect:(()=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})()}))
      };
    }""")

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "homepage": HOME, "terms": [], "discovered_topics": {}}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36", viewport={"width":1440,"height":1000}, locale="en-US")
        page = context.new_page()
        response = page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        try: page.wait_for_load_state("networkidle", timeout=12000)
        except Exception: pass
        page.wait_for_timeout(3000)
        report["homepage_diagnosis"] = {"status": response.status if response else None, "url": page.url, "title": page.title(), "dom": inspect_search_dom(page), "body_excerpt": page.locator("body").inner_text()[:2500]}

        for term in TERMS:
            events = []
            def on_request(req):
                if any(k in req.url.lower() for k in ("search","suggest","topic")):
                    events.append({"type":"request","method":req.method,"url":req.url[:2500],"resource_type":req.resource_type})
            def on_response(resp):
                if any(k in resp.url.lower() for k in ("search","suggest","topic")):
                    events.append({"type":"response","status":resp.status,"url":resp.url[:2500],"content_type":resp.headers.get("content-type","")})
            page.on("request", on_request); page.on("response", on_response)
            item = {"query":term}
            try:
                item["dom_before"] = inspect_search_dom(page)
                # Try activating the visible header search control; preserve diagnostics if it is inert.
                buttons = page.locator('button[aria-label="Search"]')
                for i in range(buttons.count()):
                    try:
                        if buttons.nth(i).is_visible():
                            buttons.nth(i).click(timeout=2500)
                            page.wait_for_timeout(500)
                            break
                    except Exception:
                        pass
                item["dom_after_button"] = inspect_search_dom(page)
                # Lenovo renders the whole header as inert/aria-hidden on this route;
                # the search input itself is enabled but Playwright correctly refuses interaction.
                # Re-enable only the header subtree for this isolated automation page.
                item["header_activation"] = page.evaluate("""() => {
                  const h = document.querySelector("header");
                  if (!h) return {found:false};
                  const before = {inert:h.hasAttribute("inert"), ariaHidden:h.getAttribute("aria-hidden")};
                  h.removeAttribute("inert"); h.removeAttribute("aria-hidden");
                  return {found:true,before,after:{inert:h.hasAttribute("inert"),ariaHidden:h.getAttribute("aria-hidden")}};
                }""")
                box = page.locator("#searchInput")
                box.fill(term, timeout=10000)
                page.wait_for_timeout(1800)
                item["suggestion_text"] = page.locator("body").inner_text()[-3500:]
                box.press("Enter", timeout=5000)
                page.wait_for_timeout(5000)
                links = topic_links(page)
                item.update({"final_url":page.url,"title":page.title(),"result_text":page.locator("body").inner_text()[:9000],"topic_links":links,"all_result_anchors":page.locator("a").evaluate_all("(els) => els.map(a => ({url:a.href,text:(a.innerText||a.textContent||'').trim().replace(/\\s+/g,' ').slice(0,300)})).filter(x => x.text && x.url.includes('lenovo.com')).slice(0,300)"),"events":events[:200]})
                for link in item["all_result_anchors"]:
                    # Search results currently link to /topic/findpost/<fid>/<tid>/<pid>.
                    # Older/community pages may use /td-p/<tid>.
                    m = re.search(r"/topic/findpost/\d+/(\d+)/(\d+)", link["url"])
                    if m:
                        tid, pid = m.group(1), m.group(2)
                    else:
                        m = re.search(r"/td-p/(\d+)", link["url"])
                        if not m:
                            continue
                        tid, pid = m.group(1), None
                    # 27Q-10 and 27QD-10 are outside the target model scope.
                    if re.search(r"27\s?q(?:d)?-?10", link["text"], re.I):
                        continue
                    entry = report["discovered_topics"].setdefault(tid, {
                        "tid": tid, "url": link["url"], "title": link["text"],
                        "query": term, "source": "rendered_search_ui", "sample_pid": pid
                    })
                    if pid and not entry.get("sample_pid"):
                        entry["sample_pid"] = pid
            except Exception as e:
                item["error"] = f"{type(e).__name__}: {e}"
                item["final_url"] = page.url
                item["dom_on_error"] = inspect_search_dom(page)
                item["events"] = events[:200]
            finally:
                page.remove_listener("request", on_request); page.remove_listener("response", on_response)
            report["terms"].append(item)
            print(json.dumps({"query":term,"url":item.get("final_url"),"links":len(item.get("topic_links",[])),"network_events":len(events),"error":item.get("error","")[:180]},ensure_ascii=False))
            page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1200)
        browser.close()
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["unique_topics_found"] = len(report["discovered_topics"])
    report["search_status"] = "success" if report["unique_topics_found"] else "needs_investigation"
    (OUT / "forum_search_discovery.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"search_status":report["search_status"],"unique_topics_found":report["unique_topics_found"],"topic_ids":list(report["discovered_topics"])},ensure_ascii=False))
    if not report["unique_topics_found"]: print("::warning::No topic IDs extracted; inspect the captured DOM and network events.")

if __name__ == "__main__": main()
