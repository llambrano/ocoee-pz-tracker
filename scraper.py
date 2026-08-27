"""
Ocoee Planning & Zoning Commission — CivicClerk scraper
Intercepts internal API calls made by the JS portal, then falls back
to HTML parsing if the API shape changes.

Output: data/meetings.json  (committed to repo, served via GitHub CDN)

Run locally:
    pip install playwright beautifulsoup4
    playwright install chromium
    python scraper.py

On GitHub Actions: triggered by cron (see scraper.yml)
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, Response

PORTAL          = "https://ocoeefl.portal.civicclerk.com"
SEARCH_TERMS    = ["Planning", "Zoning"]   # filter keywords
OUTPUT_FILE     = Path("data/meetings.json")
TIMEOUT_MS      = 30_000                   # 30 s page load timeout

# ── helpers ──────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()

# ── API interceptor ───────────────────────────────────────────────────────────

class Interceptor:
    """Captures every JSON response the portal fetches from its own backend."""

    def __init__(self):
        self.calls: list[dict] = []

    async def handle(self, response: Response):
        url = response.url
        ct  = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            body = await response.json()
            self.calls.append({"url": url, "body": body})
            print(f"  [intercept] {response.status} {url}")
        except Exception:
            pass

# ── scraper core ─────────────────────────────────────────────────────────────

async def scrape() -> list[dict]:
    meetings: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],  # needed in CI
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (compatible; OcoeePZBot/1.0; "
                "+https://github.com/YOUR_ORG/ocoee-pz-tracker)"
            )
        )
        page = await context.new_page()
        interceptor = Interceptor()
        page.on("response", interceptor.handle)

        # ── Step 1: load portal home, wait for JS hydration ──────────────────
        print("Loading portal…")
        await page.goto(PORTAL, wait_until="networkidle", timeout=TIMEOUT_MS)
        await page.wait_for_timeout(3000)   # extra buffer for lazy fetches

        # ── Step 2: dump every intercepted API call so we can inspect them ───
        print(f"\nIntercepted {len(interceptor.calls)} API call(s):")
        for call in interceptor.calls:
            print(f"  {call['url']}")

        # Save raw intercepts for debugging (optional)
        Path("data").mkdir(exist_ok=True)
        Path("data/raw_api_calls.json").write_text(
            json.dumps(interceptor.calls, indent=2, default=str)
        )

        # ── Step 3: try to parse intercepted data into meeting records ────────
        meetings = _parse_intercepted(interceptor.calls)

        if not meetings:
            # ── Step 4 fallback: scrape rendered DOM directly ─────────────────
            print("\nNo structured data from intercepts — scraping DOM…")
            meetings = await _scrape_dom(page)

        # ── Step 5: for each meeting, open its files page to get doc links ────
        print(f"\nEnriching {len(meetings)} meeting(s) with document links…")
        for m in meetings:
            if m.get("eventId"):
                await _enrich_with_docs(context, m)

        await browser.close()

    return meetings


def _parse_intercepted(calls: list[dict]) -> list[dict]:
    """
    Try to extract meeting records from intercepted API responses.
    CivicClerk typically returns arrays of event objects.
    Adapt field names here if the portal updates its schema.
    """
    meetings = []
    for call in calls:
        body = call["body"]
        items = None

        # Common shapes: {data: [...]} / {events: [...]} / [...]
        if isinstance(body, list):
            items = body
        elif isinstance(body, dict):
            for key in ("data", "events", "items", "results", "value"):
                if isinstance(body.get(key), list):
                    items = body[key]
                    break

        if not items:
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            name = (
                item.get("name") or item.get("title") or item.get("eventName") or ""
            ).lower()
            if not any(kw.lower() in name for kw in SEARCH_TERMS):
                continue

            event_id = item.get("id") or item.get("eventId") or item.get("Id")
            date_raw = (
                item.get("date") or item.get("eventDate") or
                item.get("startDate") or item.get("meetingDate") or ""
            )
            meetings.append({
                "eventId":   event_id,
                "title":     clean(item.get("name") or item.get("title")),
                "date":      date_raw,
                "status":    item.get("status") or item.get("publishState") or "unknown",
                "portalUrl": f"{PORTAL}/event/{event_id}/files" if event_id else None,
                "agendaUrl": None,
                "minutesUrl": None,
                "documents": [],
                "raw":       item,   # keep raw data for debugging
            })

    return meetings


async def _scrape_dom(page) -> list[dict]:
    """
    Fallback: parse rendered HTML for meeting cards.
    Selectors are best-effort — inspect the portal DOM and update them
    if CivicClerk changes its markup.
    """
    from bs4 import BeautifulSoup

    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    meetings = []
    # Try common card/list-item patterns
    for card in soup.select("[class*='event'], [class*='meeting'], [class*='card']"):
        text = card.get_text(" ", strip=True)
        if not any(kw.lower() in text.lower() for kw in SEARCH_TERMS):
            continue
        # Pull the nearest anchor for the event URL
        link = card.find("a", href=re.compile(r"/event/\d+"))
        event_id = None
        portal_url = None
        if link:
            m = re.search(r"/event/(\d+)", link["href"])
            if m:
                event_id = int(m.group(1))
                portal_url = f"{PORTAL}/event/{event_id}/files"

        meetings.append({
            "eventId":   event_id,
            "title":     clean(card.get_text(" ")[:120]),
            "date":      None,
            "status":    "unknown",
            "portalUrl": portal_url,
            "agendaUrl": None,
            "minutesUrl": None,
            "documents": [],
        })

    return meetings


async def _enrich_with_docs(context, meeting: dict):
    """Open the event files page and collect agenda/minutes/attachment links."""
    event_id = meeting["eventId"]
    url = f"{PORTAL}/event/{event_id}/files"
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
        await page.wait_for_timeout(2000)

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(await page.content(), "html.parser")

        docs = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            label = clean(a.get_text())
            if not label:
                continue
            full_url = href if href.startswith("http") else PORTAL + href

            doc_type = "other"
            lower = label.lower() + href.lower()
            if "agenda" in lower:
                doc_type = "agenda"
                meeting["agendaUrl"] = full_url
            elif "minute" in lower:
                doc_type = "minutes"
                meeting["minutesUrl"] = full_url
            elif any(x in lower for x in ["packet", "staff report", "attachment", "exhibit"]):
                doc_type = "attachment"

            if any(x in href for x in ["/files/", "/agenda/", "/minutes/", ".pdf"]):
                docs.append({"label": label, "url": full_url, "type": doc_type})

        meeting["documents"] = docs
        await page.close()
        print(f"  event {event_id}: {len(docs)} doc(s) found")
    except Exception as e:
        print(f"  event {event_id}: doc enrichment failed — {e}")

# ── main ─────────────────────────────────────────────────────────────────────

async def main():
    print(f"=== Ocoee P&Z scraper — {now_iso()} ===\n")
    meetings = await scrape()

    # Strip raw field before saving (keeps file small)
    for m in meetings:
        m.pop("raw", None)

    output = {
        "scrapedAt": now_iso(),
        "source":    PORTAL,
        "count":     len(meetings),
        "meetings":  meetings,
    }

    Path("data").mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, indent=2, default=str))
    print(f"\n✅  Saved {len(meetings)} meeting(s) → {OUTPUT_FILE}")

    if not meetings:
        print("⚠️  Zero meetings found — check data/raw_api_calls.json to debug.")
        sys.exit(1)   # fail CI so you get a notification


if __name__ == "__main__":
    asyncio.run(main())
