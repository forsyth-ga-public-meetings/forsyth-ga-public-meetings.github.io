"""Derive agenda content from PDF agendas.

Most Forsyth County boards -- Planning Commission, Zoning Board of Appeals,
Board of Assessors and the rest -- publish an agenda PDF and nothing else. Only
the Board of Commissioners has a structured API. Without this module those
meetings carry a date and a link and nothing a reader or a crawler can search.

What this does NOT do, deliberately:

* It does not invent item numbers. A number is recorded only when the PDF line
  actually starts with one, and derived items are marked as such so the meeting
  page can say where they came from. Structured CivicClerk items always win;
  PDF-derived items are only used when there are no structured ones.
* It does not claim a hierarchy the text does not show. Derived items are flat.
* It does not re-download an unchanged PDF. Responses are cached on disk by URL,
  and revalidated with ETag / Last-Modified.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from .config import Config
from .http import PoliteSession
from .model import AgendaItem, Document, Meeting
from .topics import apply_flags, is_consent_marker, is_consent_reset

log = logging.getLogger(__name__)

CACHE_DIRNAME = "pdf-cache"
MAX_PDF_BYTES = 40 * 1024 * 1024  # agenda packets can be enormous; skip those

# A line that opens with a recognisable agenda number: "1.", "A.", "IV.", "3)".
_NUMBERED = re.compile(
    r"^\s*(?P<num>(?:\d{1,3}|[A-Z]|[IVXLC]{1,7}))\s*[.)]\s+(?P<rest>\S.*)$"
)
_PAGE_NOISE = re.compile(
    r"^\s*(?:page\s+\d+(?:\s+of\s+\d+)?|\d+\s*\|\s*page|-\s*\d+\s*-)\s*$", re.I
)
_WS = re.compile(r"[ \t]+")


class PdfExtractionError(RuntimeError):
    """The PDF could not be read at all."""


# ---------------------------------------------------------------------------
# fetching, with an on-disk cache
# ---------------------------------------------------------------------------

def _cache_paths(data_dir: Path, url: str) -> tuple[Path, Path]:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    root = data_dir / CACHE_DIRNAME
    return root / f"{digest}.pdf", root / f"{digest}.json"


def fetch_pdf(session: PoliteSession, data_dir: Path, url: str) -> bytes | None:
    """Return PDF bytes, using a conditional request when we have it cached."""
    blob_path, meta_path = _cache_paths(data_dir, url)
    blob_path.parent.mkdir(parents=True, exist_ok=True)

    headers: dict[str, str] = {}
    meta: dict = {}
    if meta_path.exists() and blob_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if etag := meta.get("etag"):
            headers["If-None-Match"] = etag
        if lastmod := meta.get("last_modified"):
            headers["If-Modified-Since"] = lastmod

    resp = session.get(url, headers=headers)
    if resp.status_code == 304 and blob_path.exists():
        log.debug("pdf unchanged: %s", url)
        return blob_path.read_bytes()
    if resp.status_code == 404:
        log.warning("pdf missing (404): %s", url)
        return None
    if resp.status_code != 200:
        log.warning("pdf HTTP %d: %s", resp.status_code, url)
        return None

    content = resp.content
    if not content.startswith(b"%PDF"):
        log.warning("not a PDF (got %r): %s", content[:8], url)
        return None
    if len(content) > MAX_PDF_BYTES:
        log.info("skipping %s: %.1f MB exceeds the size limit",
                 url, len(content) / 1024 / 1024)
        return None

    blob_path.write_bytes(content)
    meta_path.write_text(json.dumps({
        "url": url,
        "etag": resp.headers.get("ETag"),
        "last_modified": resp.headers.get("Last-Modified"),
        "bytes": len(content),
    }, indent=2), encoding="utf-8")
    return content


# ---------------------------------------------------------------------------
# text extraction
# ---------------------------------------------------------------------------

def extract_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise PdfExtractionError("pypdf is not installed") from exc

    import io

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types
        raise PdfExtractionError(f"could not read PDF: {exc}") from exc
    return "\n".join(pages)


def clean_lines(text: str) -> list[str]:
    """Normalise extracted text into candidate agenda lines."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = _WS.sub(" ", raw).strip()
        if not line or _PAGE_NOISE.match(line):
            continue
        out.append(line)

    # Join obvious continuations. Two signals, both conservative:
    #   1. the line starts lowercase and the previous one did not end a sentence
    #   2. the previous line has an unclosed "(" -- county agendas wrap
    #      mid-citation, e.g. "... from 10 ft. to 0 ft. (UDC" / "Table 17.1)."
    # A line that carries its own item number is never treated as continuation.
    merged: list[str] = []
    for line in out:
        if merged and not _NUMBERED.match(line):
            prev = merged[-1]
            unclosed = prev.count("(") > prev.count(")")
            lowercase_run = (
                line[:1].islower() and not prev.endswith((".", ":", "?", "!"))
            )
            if unclosed or lowercase_run:
                merged[-1] = f"{prev} {line}"
                continue
        merged.append(line)
    return merged


def build_items_from_text(text: str, cfg: Config) -> list[AgendaItem]:
    """Turn extracted PDF text into flat, clearly-derived agenda items.

    Only lines that carry their own number, or that look like a heading, become
    items. Everything else is dropped rather than guessed at.
    """
    items: list[AgendaItem] = []
    under_consent = False
    consent_reason: str | None = None

    for line in clean_lines(text):
        m = _NUMBERED.match(line)
        # Section detection must run against the text WITHOUT its number:
        # a PDF line reads "V. New Business", not "New Business".
        heading_text = m.group("rest").strip() if m else line

        is_marker = is_consent_marker(heading_text, cfg)
        if is_marker:
            under_consent = True
            consent_reason = heading_text[:120]
        elif is_consent_reset(heading_text, cfg):
            under_consent = False
            consent_reason = None

        if not m:
            continue
        body = heading_text
        # Skip fragments that are clearly not agenda items.
        if len(body) < 12:
            continue

        item = AgendaItem(
            number=m.group("num"),
            outline_path=m.group("num"),
            title=body,
            level=0,
            # The heading that opens a consent block is not itself under it --
            # same rule as the structured CivicClerk path.
            under_consent=False if is_marker else under_consent,
            consent_reason=None if is_marker else consent_reason,
        )
        apply_flags(item, cfg)
        items.append(item)

    return items


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def _agenda_document(m: Meeting) -> Document | None:
    """The best candidate agenda PDF for a meeting, or None."""
    candidates = [
        d for d in m.documents
        if d.machine_readable and d.kind == "agenda"
        and (d.url.lower().endswith(".pdf") or (d.content_type or "").endswith("pdf"))
    ]
    if not candidates:
        return None
    # Prefer the county's own static PDF over an API endpoint.
    candidates.sort(key=lambda d: (d.source != "county", len(d.url)))
    return candidates[0]


def enrich(meetings: list[Meeting], session: PoliteSession, cfg: Config,
           data_dir: Path) -> int:
    """Fill in agenda items for meetings that have only a PDF. Returns count."""
    enriched = 0
    for m in meetings:
        if list(m.all_items()):
            continue  # structured items already present; never override them
        doc = _agenda_document(m)
        if doc is None:
            continue
        try:
            pdf = fetch_pdf(session, data_dir, doc.url)
            if not pdf:
                continue
            text = extract_text(pdf)
        except PdfExtractionError as exc:
            log.warning("pdf extraction failed for %s: %s", m.slug or m.date, exc)
            m.agenda_status_note = (
                "The county published this agenda only as a PDF, and its text "
                "could not be extracted. Use the PDF link below."
            )
            continue

        items = build_items_from_text(text, cfg)
        if not items:
            m.agenda_status_note = (
                "The county published this agenda only as a PDF, and no "
                "numbered items could be read from it. Use the PDF link below."
            )
            continue

        m.items = items
        m.agenda_derived_from_pdf = True
        m.agenda_published = True
        m.agenda_status_note = (
            "The items below were read automatically from the county's agenda "
            "PDF, which has no machine-readable structure. Numbering follows the "
            "PDF, but nesting and any item the extractor could not read are not "
            "shown. The PDF linked below is the complete document."
        )
        enriched += 1
        log.info("pdf: %s -> %d items", m.slug or m.date, len(items))

    return enriched
