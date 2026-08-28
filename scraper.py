"""
Ocoee Planning & Zoning Commission — CivicClerk scraper
Calls the public OData API directly. No browser required.

API base : https://ocoeefl.api.civicclerk.com/v1/
P&Z category ID : 27

Output: data/meetings.json
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

API         = "https://ocoeefl.api.civicclerk.com/v1"
CDN         = f"{API}/stream/OCOEEFL"
PORTAL      = "https://ocoeefl.portal.civicclerk.com"
PZ_CATEGORY = 27
OUTPUT_FILE = Path("data/meetings.json")

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "OcoeePZBot/1.0 (+https://github.com/llambrano/ocoee-pz-tracker)",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get(url, **params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def file_url(raw):
    if not raw:
        return None
    if raw.startswith("http"):
        return raw
    path = raw.replace("stream/OCOEEFL/", "")
    return f"{CDN}/{path}"


def fetch_pz_events():
    """Fetch ALL P&Z events, following OData pagination."""
    events = []
    url = f"{API}/Events"
    params = {
        "$filter": f"eventCategoryId eq {PZ_CATEGORY}",
        "$orderby": "startDateTime desc",
    }
    while url:
        data = get(url, **params) if params else get(url)
        params = {}
        events.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return events


def shape(event):
    published_files = event.get("publishedFiles") or []
    docs = []
    agenda_url = minutes_url = packet_url = None

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

    name = event.get("eventName", "")
    return {
        "eventId":    event["id"],
        "title":      name,
        "canceled":   name.lower().startswith("canceled"),
        "date":       event.get("startDateTime") or event.get("eventDate"),
        "status":     event.get("isPublished", ""),
        "agendaPosted": event.get("publishedAgendaTimeStamp", ""),
        "location": {
            "address": (event.get("eventLocation") or {}).get("address1", ""),
            "city":    (event.get("eventLocation") or {}).get("city", ""),
            "state":   (event.get("eventLocation") or {}).get("state", ""),
            "zip":     (event.get("eventLocation") or {}).get("zipCode", ""),
        },
        "hasAgenda":  event.get("hasAgenda", False),
        "agendaUrl":  agenda_url,
        "minutesUrl": minutes_url,
        "packetUrl":  packet_url,
        "documents":  docs,
        "portalUrl":  f"{PORTAL}/event/{event['id']}/files",
    }


def main():
    print(f"=== Ocoee P&Z scraper — {now_iso()} ===\n")
    print("Fetching Planning & Zoning events from API...")

    raw = fetch_pz_events()
    print(f"  {len(raw)} event(s) returned")

    meetings = [shape(e) for e in raw]

    today = now_iso()
    upcoming = sorted([m for m in meetings if (m["date"] or "") >= today], key=lambda m: m["date"])
    past     = sorted([m for m in meetings if (m["date"] or "") <  today], key=lambda m: m["date"], reverse=True)
    meetings = upcoming + past

    output = {
        "scrapedAt": now_iso(),
        "source":    f"{API}/Events?$filter=eventCategoryId eq {PZ_CATEGORY}",
        "count":     len(meetings),
        "meetings":  meetings,
    }

    Path("data").mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, indent=2, default=str))
    print(f"\n✅  Saved {len(meetings)} meetings → {OUTPUT_FILE}")

    for m in meetings[:8]:
        tag  = "🔴 CANCELED" if m["canceled"] else ("📅 upcoming" if m["date"] >= today else "✅ past")
        docs = len(m["documents"])
        print(f"  {tag} | {m['date'][:10]} | {m['title']} | {docs} doc(s)")

    if not meetings:
        print("⚠️  Zero meetings found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
