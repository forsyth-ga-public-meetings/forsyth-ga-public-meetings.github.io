#!/usr/bin/env python3
"""
foco_agendas.py — pull Forsyth County, GA meeting agendas out of the
JavaScript-rendered portals.

Three modes:

  discover    Launch a real browser, load a portal page, and log every
              XHR/fetch it makes. This is how you find the real API.
              Run this FIRST. Everything else is a guess until you do.

  civicclerk  Hit the CivicClerk OData API directly (fast, no browser).

  render      Generic fallback: render any URL with Playwright and dump
              the text. Works on Simbli and anything else.

  watch       Fetch, diff against last run, print what changed.

Setup:
    pip install requests playwright beautifulsoup4
    playwright install chromium

Usage:
    python foco_agendas.py discover https://forsythcoga.portal.civicclerk.com
    python foco_agendas.py civicclerk --days 45
    python foco_agendas.py render "https://simbli.eboardsolutions.com/index.aspx?S=4069"
    python foco_agendas.py watch
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TENANT = "forsythcoga"

# CivicClerk portals are a React SPA backed by an OData service on a sibling
# subdomain. The pattern below is the standard one for this platform, but
# I have NOT verified it against this tenant -- confirm with `discover`.
CIVICCLERK_API = f"https://{TENANT}.api.civicclerk.com/v1"
CIVICCLERK_PORTAL = f"https://{TENANT}.portal.civicclerk.com"

SIMBLI_BOE = "https://simbli.eboardsolutions.com/index.aspx?S=4069"

STATE_DIR = Path.home() / ".foco_agendas"
STATE_DIR.mkdir(exist_ok=True)

UA = "foco-agendas/1.0 (personal civic monitoring; contact: you@example.com)"

# Topics worth flagging. Tune to taste.
KEYWORDS = [
    # taxes
    "millage", "rollback", "tax digest", "homestead", "budget resolution",
    "LHOST", "FLOST", "SPLOST", "ESPLOST", "TSPLOST", "bond", "referendum",
    "impact fee", "assessment",
    # policing / surveillance
    "Flock", "license plate", "ALPR", "automated license", "speed camera",
    "photo enforcement", "red light", "surveillance", "camera", "drone",
    "Axon", "Motorola", "data sharing", "data-sharing", "interoperab",
    "Sheriff", "GEMA", "Homeland Security", "grant",
    # schools
    "curriculum", "instructional material", "media center", "library",
    "policy I", "Policy J", "school calendar",
]

CONSENT_MARKERS = ["consent agenda", "consent calendar", "ratification of the following"]


# ---------------------------------------------------------------------------
# discover — find the real API
# ---------------------------------------------------------------------------

def cmd_discover(args):
    """Load a page in a real browser and log every network call it makes."""
    from playwright.sync_api import sync_playwright

    url = args.url
    seen = []

    def on_request(req):
        if req.resource_type in ("xhr", "fetch"):
            seen.append((req.method, req.url))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.on("request", on_request)
        page.goto(url, wait_until="networkidle", timeout=60_000)
        # Some portals lazy-load the calendar; give it a nudge.
        page.wait_for_timeout(3000)
        browser.close()

    if not seen:
        print("No XHR/fetch observed. Page may be server-rendered -- try `render`.")
        return

    print(f"\n{len(seen)} API call(s) observed loading {url}:\n")
    for method, u in dict.fromkeys(seen):  # dedupe, keep order
        print(f"  {method:6} {u}")
    print(
        "\nLook for one returning JSON with meeting names/dates. That is your\n"
        "endpoint. Paste it into a browser tab -- OData endpoints usually\n"
        "respond to plain GETs with no auth.\n"
    )


# ---------------------------------------------------------------------------
# civicclerk — OData
# ---------------------------------------------------------------------------

def civicclerk_events(days_back=21, days_fwd=45, verbose=True):
    """Fetch events in a date window from the CivicClerk OData API."""
    now = datetime.now(timezone.utc)
    lo = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00Z")
    hi = (now + timedelta(days=days_fwd)).strftime("%Y-%m-%dT00:00:00Z")

    params = {
        "$filter": f"startDateTime ge {lo} and startDateTime le {hi}",
        "$orderby": "startDateTime asc",
        "$top": "200",
    }
    url = f"{CIVICCLERK_API}/Events"

    r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=30)
    if r.status_code != 200:
        print(f"HTTP {r.status_code} from {r.url}", file=sys.stderr)
        print(
            "\nThe endpoint guess was wrong, or the schema differs.\n"
            f"Run: python {sys.argv[0]} discover {CIVICCLERK_PORTAL}\n"
            "and use whatever URL actually shows up.\n",
            file=sys.stderr,
        )
        r.raise_for_status()

    data = r.json()
    events = data.get("value", data if isinstance(data, list) else [])
    if verbose:
        print(f"{len(events)} events between {lo[:10]} and {hi[:10]}\n")
    return events


def civicclerk_agenda_text(event_id):
    """
    Try to pull the agenda body for an event.

    Agenda item text on CivicClerk lives under a per-event files/agenda path.
    The portal URL that works in a browser is:
        {portal}/event/{event_id}/files/agenda/{agenda_id}
    Confirm the API shape with `discover` on one of those pages.
    """
    for path in (
        f"{CIVICCLERK_API}/Meetings/GetMeetingFileStream?fileId={event_id}&plainText=true",
        f"{CIVICCLERK_API}/Events({event_id})/publishedFiles",
        f"{CIVICCLERK_API}/Events({event_id})?$expand=eventItems",
    ):
        try:
            r = requests.get(path, headers={"User-Agent": UA}, timeout=30)
            if r.status_code == 200 and r.text.strip():
                return r.text
        except requests.RequestException:
            continue
        time.sleep(0.5)  # be polite
    return ""


def cmd_civicclerk(args):
    events = civicclerk_events(days_back=args.back, days_fwd=args.days)
    for ev in events:
        eid = ev.get("id")
        name = ev.get("eventName") or ev.get("categoryName") or "(untitled)"
        when = (ev.get("startDateTime") or "")[:16].replace("T", " ")
        print(f"[{eid}] {when}  {name}")
        print(f"        {CIVICCLERK_PORTAL}/event/{eid}/files")

        if args.full:
            body = civicclerk_agenda_text(eid)
            for line in flag_lines(body):
                print(f"        !! {line}")
        print()


# ---------------------------------------------------------------------------
# render — generic Playwright fallback
# ---------------------------------------------------------------------------

def render(url, wait_selector=None, timeout=60_000):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.goto(url, wait_until="networkidle", timeout=timeout)
        if wait_selector:
            page.wait_for_selector(wait_selector, timeout=timeout)
        page.wait_for_timeout(2000)
        html = page.content()
        text = page.inner_text("body")
        browser.close()
    return html, text


def cmd_render(args):
    html, text = render(args.url, wait_selector=args.selector)
    if args.html:
        print(html)
    else:
        print(text)
        hits = flag_lines(text)
        if hits:
            print("\n--- flagged ---")
            for h in hits:
                print(f"  !! {h}")


# ---------------------------------------------------------------------------
# keyword flagging
# ---------------------------------------------------------------------------

def flag_lines(text):
    """Return lines matching a watch keyword, plus consent-agenda context."""
    if not text:
        return []
    out = []
    in_consent = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if any(m in low for m in CONSENT_MARKERS):
            in_consent = True
        # crude: a top-level heading resets consent context
        elif re.match(r"^(new business|old business|public hearing)", low):
            in_consent = False

        if any(k.lower() in low for k in KEYWORDS):
            money = re.findall(r"\$[\d,]+(?:\.\d{2})?", line)
            tag = "[CONSENT] " if in_consent else ""
            big = ""
            for m in money:
                try:
                    if float(m[1:].replace(",", "")) >= 100_000:
                        big = " <-- LARGE $"
                        break
                except ValueError:
                    pass
            out.append(f"{tag}{line}{big}")
    return out


# ---------------------------------------------------------------------------
# watch — diff against last run
# ---------------------------------------------------------------------------

def cmd_watch(args):
    """Fetch current state, diff vs. last run, report changes."""
    events = civicclerk_events(days_back=7, days_fwd=45, verbose=False)
    current = {
        str(ev.get("id")): {
            "name": ev.get("eventName") or ev.get("categoryName"),
            "start": ev.get("startDateTime"),
        }
        for ev in events
    }

    statefile = STATE_DIR / "civicclerk.json"
    previous = {}
    if statefile.exists():
        previous = json.loads(statefile.read_text())

    new_ids = set(current) - set(previous)
    changed = {
        k for k in set(current) & set(previous) if current[k] != previous[k]
    }

    if not new_ids and not changed:
        print("No changes.")
    else:
        for k in sorted(new_ids, key=lambda x: current[x]["start"] or ""):
            e = current[k]
            print(f"NEW      {(e['start'] or '')[:16]}  {e['name']}")
            print(f"         {CIVICCLERK_PORTAL}/event/{k}/files")
        for k in sorted(changed):
            print(f"CHANGED  {current[k]['name']}")
            print(f"         was: {previous[k]}")
            print(f"         now: {current[k]}")

    statefile.write_text(json.dumps(current, indent=2))


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="log a page's XHR/fetch calls")
    d.add_argument("url", nargs="?", default=CIVICCLERK_PORTAL)
    d.set_defaults(func=cmd_discover)

    c = sub.add_parser("civicclerk", help="query the CivicClerk OData API")
    c.add_argument("--days", type=int, default=45, help="days forward")
    c.add_argument("--back", type=int, default=21, help="days back")
    c.add_argument("--full", action="store_true", help="also fetch agenda bodies")
    c.set_defaults(func=cmd_civicclerk)

    r = sub.add_parser("render", help="render any URL with a headless browser")
    r.add_argument("url")
    r.add_argument("--selector", help="CSS selector to wait for")
    r.add_argument("--html", action="store_true", help="dump HTML not text")
    r.set_defaults(func=cmd_render)

    w = sub.add_parser("watch", help="diff against last run")
    w.set_defaults(func=cmd_watch)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
