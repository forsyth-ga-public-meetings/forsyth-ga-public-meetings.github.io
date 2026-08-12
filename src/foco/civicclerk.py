"""CivicClerk OData fetcher (Board of Commissioners).

Verified live 2026-08-11. Two things this module exists to get right:

1. Agenda items hang off `Meetings/{agendaId}`, NOT `Meetings/{eventId}`.
   Using the event id returns 404.
2. CivicClerk stamps local Eastern wall-clock time with a "Z" suffix. The API
   reports 17:00:00Z for a meeting the county advertises as 5:00 PM. Treating
   those as real UTC shifts every meeting by four hours.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config
from .http import PoliteSession
from .model import AgendaItem, Document, Meeting, split_body_and_type
from .topics import apply_flags, clean_text, is_consent_marker

log = logging.getLogger(__name__)


class ParseError(RuntimeError):
    """The vendor's payload no longer looks the way we expect. Fail loudly."""


def parse_civicclerk_datetime(raw: str | None, cfg: Config) -> datetime | None:
    """Parse CivicClerk's fake-UTC timestamps into real localized datetimes."""
    if not raw:
        return None
    text = raw.rstrip("Z")
    try:
        naive = datetime.fromisoformat(text)
    except ValueError:
        return None
    naive = naive.replace(tzinfo=None)
    if cfg.sources["civicclerk"].get("timestamps_are_local_wall_time", True):
        return naive.replace(tzinfo=ZoneInfo(cfg.local_tz))
    return naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(cfg.local_tz))


def attachment_url(api_base: str, file_id: int) -> str:
    """Direct PDF for one agenda-item attachment.

    Verified: returns application/pdf bytes, unsigned and non-expiring. The
    `pdfVersionFullPath` in the payload is an Azure blob URL with a signature
    that expires in about a week -- baking that into a static page would produce
    dead links, so we never use it.
    """
    return f"{api_base}/Meetings/GetAttachmentFile(fileId={file_id})"


def meeting_file_url(api_base: str, file_id: int, *, plain_text: bool = False) -> str:
    """Direct bytes for a published meeting file.

    Note GetMeetingFile (no 'Stream') returns a JSON envelope containing an
    expiring blobUri, not the file. GetMeetingFileStream returns the bytes.
    """
    flag = "true" if plain_text else "false"
    return f"{api_base}/Meetings/GetMeetingFileStream(fileId={file_id},plainText={flag})"


def build_attachments(raw: dict, api_base: str) -> list[Document]:
    out: list[Document] = []
    for att in raw.get("attachmentsList") or []:
        if att.get("isDeleted") or att.get("isPublished") is False:
            continue
        file_id = att.get("id")
        if file_id is None:
            continue
        label = clean_text(att.get("fileName")) or clean_text(
            att.get("mediaFileName")
        ) or f"Attachment {file_id}"
        out.append(
            Document(
                label=label,
                url=attachment_url(api_base, file_id),
                kind="attachment",
                source="civicclerk",
                machine_readable=True,
                content_type=att.get("contentType") or None,
                size_bytes=att.get("fileSize") or None,
            )
        )
    return out


def _outline(item: dict) -> str | None:
    """The item's own number as published, or None. Never invented."""
    for key in ("agendaObjectItemOutlineNumberFull", "agendaObjectItemOutlineNumber"):
        val = (item.get(key) or "").strip()
        if val:
            return val.rstrip(".")
    return None


def build_items(
    raw_items: list[dict],
    cfg: Config,
    *,
    api_base: str | None = None,
    level: int = 0,
    parent_path: str | None = None,
    under_consent: bool = False,
    consent_reason: str | None = None,
) -> list[AgendaItem]:
    """Convert CivicClerk's nested item payload into AgendaItems, with flags."""
    if api_base is None:
        api_base = cfg.sources["civicclerk"]["api_base"]
    out: list[AgendaItem] = []
    for raw in sorted(raw_items, key=lambda r: r.get("sortOrder") or 0):
        title = clean_text(raw.get("agendaObjectItemName"))
        number = _outline(raw)
        path = ".".join(p for p in (parent_path, number) if p) or None

        # Consent status is inherited: an item under a "Consent Agenda" or
        # "Board Ratification of the following" heading passes without debate.
        item_is_marker = is_consent_marker(title, cfg)
        child_consent = under_consent or item_is_marker
        child_reason = consent_reason or (title if item_is_marker else None)

        item = AgendaItem(
            number=number,
            outline_path=path,
            title=title,
            level=level,
            source_item_id=raw.get("id"),
            fiscal_impact=clean_text(raw.get("fiscalImpactSummary")) or None,
            attachments=build_attachments(raw, api_base),
            # The heading itself is not "under" consent; its children are.
            under_consent=under_consent,
            consent_reason=consent_reason,
        )
        apply_flags(item, cfg)
        item.children = build_items(
            raw.get("childItems") or [],
            cfg,
            api_base=api_base,
            level=level + 1,
            parent_path=path,
            under_consent=child_consent,
            consent_reason=child_reason,
        )
        out.append(item)
    return out


def fetch_events(session: PoliteSession, cfg: Config) -> list[dict]:
    """Events within the configured window. CivicClerk exposes no future events."""
    base = cfg.sources["civicclerk"]["api_base"]
    today = datetime.now(ZoneInfo(cfg.local_tz)).date()
    lo = (today - timedelta(days=cfg.days_back)).isoformat()
    hi = (today + timedelta(days=cfg.days_forward)).isoformat()
    params = {
        "$filter": f"startDateTime ge {lo}T00:00:00Z and startDateTime le {hi}T23:59:59Z",
        "$orderby": "startDateTime asc",
        "$top": "500",
    }
    payload = session.get_json(f"{base}/Events", params=params)
    if not isinstance(payload, dict) or "value" not in payload:
        raise ParseError(
            f"CivicClerk /Events did not return an OData envelope; got "
            f"{type(payload).__name__} with keys "
            f"{list(payload)[:8] if isinstance(payload, dict) else 'n/a'}"
        )
    return payload["value"]


def fetch_agenda(session: PoliteSession, cfg: Config, agenda_id: int) -> dict | None:
    """Agenda payload for an agendaId, or None when not published."""
    base = cfg.sources["civicclerk"]["api_base"]
    resp = session.get(f"{base}/Meetings/{agenda_id}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def build_meeting(event: dict, agenda: dict | None, cfg: Config) -> Meeting:
    cc = cfg.sources["civicclerk"]
    portal_base = cc["portal_base"]
    api_base = cc["api_base"]

    name = clean_text(event.get("eventName")) or clean_text(event.get("categoryName"))
    body, mtype = split_body_and_type(clean_text(name))
    start = parse_civicclerk_datetime(event.get("startDateTime"), cfg)
    if start is None:
        raise ParseError(
            f"CivicClerk event {event.get('id')} has an unparseable "
            f"startDateTime: {event.get('startDateTime')!r}"
        )

    event_id = event.get("id")
    agenda_id = event.get("agendaId")

    docs: list[Document] = []
    # The portal page is the authoritative human-facing source, but it is a JS
    # app -- explicitly not machine readable.
    docs.append(
        Document(
            label="CivicClerk portal page for this meeting",
            url=f"{portal_base}/event/{event_id}/files",
            kind="portal",
            source="civicclerk",
            machine_readable=False,
        )
    )

    items: list[AgendaItem] = []
    published = False
    note: str | None = None

    if agenda is None:
        note = "The county has not published an agenda for this meeting yet."
    else:
        published = bool(agenda.get("agendaIsPublish"))
        raw_items = agenda.get("items")
        if raw_items is None:
            raise ParseError(
                f"CivicClerk agenda {agenda_id} has no 'items' key; "
                f"keys were {sorted(agenda)[:12]}"
            )
        items = build_items(raw_items, cfg, api_base=api_base)
        if not items:
            note = "The county's agenda for this meeting is published but empty."
        for f in agenda.get("publishedFiles") or []:
            file_id = f.get("fileId")
            if file_id is None:
                continue
            ftype = (f.get("type") or "").lower()
            kind = (
                "packet" if "packet" in ftype
                else "minutes" if "minute" in ftype
                else "agenda" if "agenda" in ftype
                else "other"
            )
            label = clean_text(f.get("name")) or f.get("type") or "Document"
            docs.append(
                Document(
                    label=label,
                    url=meeting_file_url(api_base, file_id),
                    kind=kind,
                    source="civicclerk",
                    machine_readable=True,
                    content_type="application/pdf",
                )
            )
            # A plain-text rendering of the same file. This is the single most
            # useful URL on the page for a client that cannot read PDFs.
            if kind in ("agenda", "minutes"):
                docs.append(
                    Document(
                        label=f"{label} (plain text)",
                        url=meeting_file_url(api_base, file_id, plain_text=True),
                        kind="transcript",
                        source="civicclerk",
                        machine_readable=True,
                        content_type="text/plain",
                    )
                )

    return Meeting(
        slug="",  # assigned by the cache, which guarantees permanence
        body=body,
        meeting_type=mtype,
        title=name,
        date=start.date().isoformat(),
        start_time=start.strftime("%H:%M"),
        start_iso=start.isoformat(),
        location=None,  # CivicClerk leaves eventLocation empty for this tenant
        sources=["civicclerk"],
        civicclerk_event_id=event_id,
        civicclerk_agenda_id=agenda_id,
        portal_url=f"{portal_base}/event/{event_id}/files",
        documents=docs,
        items=items,
        agenda_published=published,
        agenda_status_note=note,
    )


def fetch_all(session: PoliteSession, cfg: Config) -> list[Meeting]:
    events = fetch_events(session, cfg)
    log.info("civicclerk: %d events in window", len(events))
    meetings: list[Meeting] = []
    for event in events:
        agenda_id = event.get("agendaId")
        agenda = None
        if agenda_id:
            agenda = fetch_agenda(session, cfg, agenda_id)
        meetings.append(build_meeting(event, agenda, cfg))
    return meetings
