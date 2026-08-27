"""
Ocoee Planning & Zoning Commission — CivicClerk scraper
Calls the OData API directly (no browser / Playwright needed).

API base : https://ocoeefl.api.civicclerk.com/v1/
P&Z category ID : 27
File CDN base   : https://ocoeefl.api.civicclerk.com/v1/stream/OCOEEFL/

Output: data/meetings.json
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── constants ─────────────────────────────────────────────────────────────────
API          = "https://ocoeefl.api.civicclerk.com/v1"
CDN          = f"{API}/stream/OCOEEFL"
PORTAL       = "https://ocoeefl.portal.civicclerk.com"
PZ_CATEGORY  = 27          # Planning & Zoning Commission
OUTPUT_FILE  = Path("data/meetings.json")

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "OcoeePZBot/1.0 (+https://github.com/llambrano/ocoee-pz-tracker)",
}

# ── helpers ───────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def get(url: str, **params) -> dict:
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def file_url(raw_url: str) -> str:
    """Convert a raw 'stream/OCOEEFL/uuid.pdf' path to a full URL."""
    if raw_url.startswith("http"):
        return raw_url
    # strip leading 'stream/OCOEEFL/' if present
    path = raw_url.replace("stream/OCOEEFL/", "")
    return f"{CDN}/{path}"

def portal_url(event_id: int) -> str:
    return f"{PORTAL}/event/{event_id}/files"

# ── fetch all P&Z events (paginated) ─────────────────────────────────────────

def fetch_pz_events() -> list[dict]:
    """
    Fetch ALL past + upcoming P&Z events from the OData API.
    Handles @odata.nextLink pagination automatically.
    """
    events = []

    # past events (newest first)
    url = f"{API}/Events"
    params = {
        "$filter": f"eventCategoryId eq {PZ_CATEGORY}",
        "$orderby": "startDateTime desc",
    }

    while url:
        data = get(url, **params) if params else get(url)
        params = {}                          # params only on first call
        events.extend(data.get("value", []))
        url = data.get("@odata.nextLink")    # follow pagination

    return events

# ── shape a raw API event into a clean record ─────────────────────────────────

def shape(event: dict) -> dict:
    published_files = event.get("publishedFiles") or []

    docs = []
    agenda_url  = None
    minutes_url = None
    packet_url  = None

    for f in published_files:
        ftype = (f.get("type") or "").strip()
        fname = (f.get("name") or "").strip()
        furl  = file_url(f.get("url", ""))

        docs.append({"label": fname, "type": ftype, "url": furl})

        if ftype == "Agenda" and not agenda_url:
            agenda_url = furl
        elif ftype == "Minutes" and not minutes_url:
            minutes_url = furl
        elif ftype == "Agenda Packet" and not packet_url:
            packet_url = furl

    event_id   = event["id"]
    name       = event.get("eventName", "")
    canceled   = name.lower().startswith("canceled")
    start_dt   = event.get("startDateTime") or event.get("eventDate")

    return {
        "eventId":      event_id,
        "title":        name,
        "canceled":     canceled,
        "date":         start_dt,
        "status":       event.get("isPublished", ""),
        "agendaPosted": event.get("publishedAgendaTimeStamp", ""),
        "categoryId":   event.get("eventCategoryId"),
        "category":     event.get("eventCategoryName"),
        "location": {
            "address": event.get("eventLocation", {}).get("address1", ""),
            "city":    event.get("eventLocation", {}).get("city", ""),
            "state":   event.get("eventLocation", {}).get("state", ""),
            "zip":     event.get("eventLocation", {}).get("zipCode", ""),
        },
        "hasAgenda":    event.get("hasAgenda", False),
        "agendaUrl":    agenda_url,
        "minutesUrl":   minutes_url,
        "packetUrl":    packet_url,
        "documents":    docs,
        "portalUrl":    portal_url(event_id),
    }

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"=== Ocoee P&Z scraper (direct API) — {now_iso()} ===\n")

    print("Fetching Planning & Zoning events…")
    raw_events = fetch_pz_events()
    print(f"  {len(raw_events)} event(s) returned from API")

    meetings = [shape(e) for e in raw_events]

    # sort: upcoming first, then past (descending)
    today = now_iso()
    upcoming = sorted([m for m in meetings if (m["date"] or "") >= today],
                      key=lambda m: m["date"])
    past     = sorted([m for m in meetings if (m["date"] or "") <  today],
                      key=lambda m: m["date"], reverse=True)
    meetings = upcoming + past

    output = {
        "scrapedAt": now_iso(),
        "source":    f"{API}/Events?$filter=eventCategoryId eq {PZ_CATEGORY}",
        "categoryId": PZ_CATEGORY,
        "count":     len(meetings),
        "meetings":  meetings,
    }

    Path("data").mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, indent=2, default=str))
    print(f"\n✅  Saved {len(meetings)} meeting(s) → {OUTPUT_FILE}")

    for m in meetings[:5]:
        flag = "🔴 CANCELED" if m["canceled"] else ("📅" if m["date"] >= today else "✅")
        docs = len(m["documents"])
        print(f"  {flag} [{m['eventId']}] {m['title']} ({m['date'][:10]}) — {docs} doc(s)")

    if not meetings:
        print("⚠️  Zero meetings — check the API filter.")
        sys.exit(1)


if __name__ == "__main__":
    main()
