"""Merge the two sources, assign permanent slugs, persist the cache.

Slug permanence is a hard requirement: an existing meeting page must never be
renumbered or re-slugged. We therefore keep a ledger mapping stable source
identities to slugs. Once a slug is in the ledger it is never recomputed, even
if the county later edits the meeting's title.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .model import Meeting, slugify

log = logging.getLogger(__name__)

CACHE_FILE = "meetings.json"
LEDGER_FILE = "slugs.json"


# ---------------------------------------------------------------------------
# identity & merging
# ---------------------------------------------------------------------------

def identity_keys(m: Meeting) -> list[str]:
    keys = []
    if m.civicclerk_event_id is not None:
        keys.append(f"cc:{m.civicclerk_event_id}")
    if m.county_meeting_id is not None:
        keys.append(f"county:{m.county_meeting_id}:{m.date}")
    if not keys:
        keys.append(f"dt:{m.date}:{slugify(m.title)}")
    return keys


def _body_compatible(a: str, b: str) -> bool:
    """Do two body labels plausibly name the same board?"""
    sa, sb = slugify(a), slugify(b)
    if not sa or not sb:
        return False
    if sa == sb or sa in sb or sb in sa:
        return True
    # Compare leading two words, e.g. "board-of-commissioners" vs "board-of-..."
    pa = "-".join(sa.split("-")[:3])
    pb = "-".join(sb.split("-")[:3])
    return bool(pa) and pa == pb


def merge(county: list[Meeting], civicclerk: list[Meeting]) -> list[Meeting]:
    """County listing is the spine; CivicClerk supplies agenda detail.

    Join on (date, start_time) with a body-compatibility guard. Anything that
    fails to join is kept on its own rather than dropped.
    """
    merged: list[Meeting] = []
    unclaimed = list(civicclerk)

    for base in county:
        match = None
        for cand in unclaimed:
            if cand.date != base.date:
                continue
            if base.start_time and cand.start_time and base.start_time != cand.start_time:
                continue
            if not _body_compatible(base.body, cand.body):
                continue
            match = cand
            break

        if match is None:
            merged.append(base)
            continue

        unclaimed.remove(match)
        base.sources = sorted(set(base.sources) | set(match.sources))
        base.civicclerk_event_id = match.civicclerk_event_id
        base.civicclerk_agenda_id = match.civicclerk_agenda_id
        base.portal_url = base.portal_url or match.portal_url
        base.items = match.items or base.items
        base.agenda_published = base.agenda_published or match.agenda_published
        base.agenda_status_note = match.agenda_status_note if not base.items else None
        base.location = base.location or match.location
        base.start_time = base.start_time or match.start_time
        base.start_iso = base.start_iso or match.start_iso
        # Keep both sources' documents, de-duplicated by URL.
        by_url = {d.url: d for d in base.documents}
        for d in match.documents:
            by_url.setdefault(d.url, d)
        base.documents = list(by_url.values())
        merged.append(base)

    if unclaimed:
        log.info("civicclerk: %d events had no county counterpart", len(unclaimed))
    merged.extend(unclaimed)
    merged.sort(key=lambda m: (m.date, m.start_time or "", m.body))
    return merged


# ---------------------------------------------------------------------------
# slugs
# ---------------------------------------------------------------------------

def load_ledger(data_dir: Path) -> dict[str, str]:
    path = data_dir / LEDGER_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_ledger(data_dir: Path, ledger: dict[str, str]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / LEDGER_FILE).write_text(
        json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8"
    )


def assign_slugs(meetings: list[Meeting], ledger: dict[str, str]) -> dict[str, str]:
    """Give every meeting a permanent slug, reusing the ledger where possible."""
    taken = set(ledger.values())
    for m in meetings:
        keys = identity_keys(m)
        existing = next((ledger[k] for k in keys if k in ledger), None)
        if existing:
            m.slug = existing
        else:
            parts = [m.date, slugify(m.body)]
            if m.meeting_type:
                parts.append(slugify(m.meeting_type))
            base = "-".join(p for p in parts if p)
            slug, n = base, 2
            while slug in taken:
                slug = f"{base}-{n}"
                n += 1
            m.slug = slug
            taken.add(slug)
        for k in keys:
            ledger[k] = m.slug
    return ledger


# ---------------------------------------------------------------------------
# hashing & persistence
# ---------------------------------------------------------------------------

def content_hash(m: Meeting) -> str:
    """Hash of everything a reader would notice changing."""
    payload = {
        "date": m.date,
        "start": m.start_iso,
        "body": m.body,
        "type": m.meeting_type,
        "location": m.location,
        "published": m.agenda_published,
        "docs": sorted(d.url for d in m.documents),
        "items": [
            [i.outline_path, i.title, i.under_consent, i.large_dollar]
            for i in m.all_items()
        ],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stamp(meetings: list[Meeting], cfg: Config) -> None:
    now = datetime.now(ZoneInfo(cfg.local_tz)).isoformat(timespec="seconds")
    for m in meetings:
        m.fetched_at = now
        m.content_hash = content_hash(m)


def save(meetings: list[Meeting], data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / CACHE_FILE
    path.write_text(
        json.dumps([m.to_dict() for m in meetings], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load(data_dir: Path) -> list[Meeting]:
    path = data_dir / CACHE_FILE
    if not path.exists():
        return []
    return [Meeting.from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))]


def archive(previous: list[Meeting], fresh: list[Meeting]) -> list[Meeting]:
    """Union the newly fetched meetings with everything we already knew.

    The fetch window (±60 days) governs what we ASK the sources for, not what
    the site keeps. Without this, a meeting older than the window would vanish
    from the next build -- and because CI builds into an empty directory, its
    page would disappear from the published site, breaking the project's
    permanent-URL rule.

    Freshly fetched data always wins; older meetings are carried forward
    untouched, including their original fetched_at, since we did not re-fetch
    them and must not imply we did.
    """
    by_slug = {m.slug: m for m in previous if m.slug}
    for m in fresh:
        by_slug[m.slug] = m
    return sorted(by_slug.values(), key=lambda m: (m.date, m.start_time or "", m.body))


CHANGELOG_FILE = "changelog.json"
CHANGELOG_LIMIT = 500


def describe_change(before: Meeting | None, after: Meeting) -> str:
    """A one-line, human-readable account of what changed. Never speculative."""
    if before is None:
        if after.items:
            n = len(list(after.all_items()))
            consent = sum(1 for i in after.all_items() if i.under_consent)
            bits = f"{n} agenda items"
            if consent:
                bits += f", {consent} under a consent agenda"
            return f"Added to the index with {bits}."
        return "Added to the index. No agenda published yet."

    old_items = list(before.all_items())
    new_items = list(after.all_items())
    parts: list[str] = []
    if not old_items and new_items:
        parts.append(f"Agenda published: {len(new_items)} items")
    elif len(old_items) != len(new_items):
        parts.append(f"Agenda items changed: {len(old_items)} to {len(new_items)}")
    elif [i.title for i in old_items] != [i.title for i in new_items]:
        parts.append("Agenda item wording changed")

    old_docs = {d.url for d in before.documents}
    new_docs = {d.url for d in after.documents}
    if added := len(new_docs - old_docs):
        parts.append(f"{added} document{'s' if added != 1 else ''} added")
    if removed := len(old_docs - new_docs):
        parts.append(f"{removed} document{'s' if removed != 1 else ''} removed")

    if before.start_iso != after.start_iso:
        parts.append("Meeting time changed")
    if before.location != after.location:
        parts.append("Location changed")

    return "; ".join(parts) + "." if parts else "Details changed."


def append_changelog(data_dir: Path, changes: dict[str, list], when: str) -> list[dict]:
    """Record this run's changes. RSS and the diff report both read from here."""
    entries = load_changelog(data_dir)
    fresh: list[dict] = []

    for m in sorted(changes["added"], key=lambda x: x.date):
        fresh.append({
            "at": when, "kind": "new", "slug": m.slug, "date": m.date,
            "title": f"{m.body}" + (f" — {m.meeting_type}" if m.meeting_type else ""),
            "summary": describe_change(None, m),
        })
    for before, after in sorted(changes["changed"], key=lambda p: p[1].date):
        fresh.append({
            "at": when, "kind": "changed", "slug": after.slug, "date": after.date,
            "title": f"{after.body}"
                     + (f" — {after.meeting_type}" if after.meeting_type else ""),
            "summary": describe_change(before, after),
        })

    # Newest first, capped so the file cannot grow without bound.
    combined = fresh + entries
    combined = combined[:CHANGELOG_LIMIT]
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / CHANGELOG_FILE).write_text(
        json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return fresh


def load_changelog(data_dir: Path) -> list[dict]:
    path = data_dir / CHANGELOG_FILE
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def diff(old: list[Meeting], new: list[Meeting]) -> dict[str, list]:
    """What changed between two cache generations."""
    old_by = {m.slug: m for m in old}
    new_by = {m.slug: m for m in new}
    added = [new_by[s] for s in new_by.keys() - old_by.keys()]
    removed = [old_by[s] for s in old_by.keys() - new_by.keys()]
    changed = [
        (old_by[s], new_by[s])
        for s in old_by.keys() & new_by.keys()
        if old_by[s].content_hash != new_by[s].content_hash
    ]
    return {"added": added, "removed": removed, "changed": changed}
