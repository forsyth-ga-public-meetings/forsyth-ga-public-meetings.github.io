"""Data model for meetings and agenda items.

Every field that can be absent at the source is Optional and stays None. We never
substitute a placeholder that could be mistaken for real data -- a missing agenda
number renders as missing, not as an invented one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field, fields
from typing import Any


def _known(cls, d: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the dataclass no longer has.

    The cache on disk outlives any single version of this code. A field that is
    renamed or promoted to a property must not make an existing cache
    unloadable -- that would turn a schema tweak into a full re-fetch.
    """
    allowed = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in allowed}


def slugify(text: str) -> str:
    """Lowercase ASCII slug. Stable: used to build permanent URLs."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return re.sub(r"-{2,}", "-", text)


# Meeting-type suffixes, longest first so "Work Session Meeting" beats
# "Work Session". Both spacings of the slash appear in the wild: CivicClerk
# writes "Regular Meeting / Public Hearings", the county writes
# "Regular Meeting/Public Hearings".
MEETING_TYPES = (
    # Canonical spellings. Comparison is whitespace-insensitive around
    # punctuation, so "Regular Meeting/Public Hearings" also matches the first
    # entry and both vendors collapse onto one label -- and therefore one slug.
    "Regular Meeting / Public Hearings",
    "Special Called Joint Meeting",
    "Special Called Public Hearing",
    "Special Called Meeting",
    "Joint Training Session",
    "Work Session Meeting",
    "Executive Session",
    "Training Session",
    "Regular Meeting",
    "Called Meeting",
    "Public Hearing",
    "Public Meeting",
    "Work Session",
    # Generic last resort. Only stripped when a substantial body name remains,
    # so "Parks & Recreation Board Meeting" loses it but "TriPartite Meeting"
    # keeps it -- there the word is part of the body's actual name.
    "Meeting",
)

_GENERIC_TYPES = {"meeting"}


def _norm(text: str) -> str:
    """Collapse whitespace and punctuation spacing for comparison.

    Also drops trailing punctuation: the county's listing contains titles like
    "... – Special Called Meeting ." whose stray period would otherwise defeat
    the match and promote the whole string to its own body name.
    """
    text = re.sub(r"\s+", " ", (text or "").lower()).strip()
    text = re.sub(r"[\s.,;:]+$", "", text)
    return re.sub(r"\s*([/&,-])\s*", r"\1", text)


def split_body_and_type(name: str) -> tuple[str, str | None]:
    """Split 'Board of Commissioners Work Session' -> (body, type).

    Comparison ignores spacing around slashes so the two vendors' spellings of
    the same meeting type collapse to one. Returns the whole string as the body
    when no known type is present, rather than guessing.
    """
    text = re.sub(r"\s+", " ", (name or "")).strip().rstrip(" .,;:")
    if not text:
        return text, None

    # Candidate split points: the start of every word, plus end-of-string.
    starts = [0] + [m.start() for m in re.finditer(r"(?<= )\S", text)]

    for mtype in sorted(MEETING_TYPES, key=len, reverse=True):
        ntype = _norm(mtype)
        if not ntype:
            continue
        for i in starts:
            tail = text[i:]
            if _norm(tail) != ntype:
                continue
            body = text[:i].strip(" -–—/,.")
            if not body:
                return text, mtype
            # A generic type is only a type when a real body name survives it.
            if ntype in _GENERIC_TYPES and len(body.split()) < 2:
                continue
            # Canonicalise to the spelling in MEETING_TYPES so the two
            # vendors' variants produce one slug.
            return body, mtype
    return text, None


@dataclass
class Document:
    """A file linked from a meeting or an agenda item."""

    label: str
    url: str
    kind: str  # "agenda" | "packet" | "notice" | "minutes" | "portal" |
               # "attachment" | "transcript" | "other"
    source: str  # "civicclerk" | "county"
    # True when this URL returns the document's bytes directly to a client that
    # cannot run JavaScript. False for the CivicClerk portal SPA, and for any
    # endpoint that only returns a JSON pointer to the real file.
    machine_readable: bool = True
    # Media type as advertised by the source, when known.
    content_type: str | None = None
    size_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgendaItem:
    """One line of an agenda, possibly with children."""

    # Original numbering exactly as published. None when the source has none.
    number: str | None
    # Full dotted path built from ancestry, e.g. "VII.1.H". None if unnumberable.
    outline_path: str | None
    title: str
    level: int = 0
    source_item_id: int | None = None
    fiscal_impact: str | None = None
    # Supporting documents for this specific item, each a directly fetchable URL.
    attachments: list["Document"] = field(default_factory=list)

    # Flags
    under_consent: bool = False
    consent_reason: str | None = None
    dollar_amounts: list[int] = field(default_factory=list)
    large_dollar: bool = False
    long_term: bool = False
    long_term_reason: str | None = None
    topics: list[str] = field(default_factory=list)

    children: list["AgendaItem"] = field(default_factory=list)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    @property
    def attachment_count(self) -> int:
        return len(self.attachments)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["children"] = [c.to_dict() for c in self.children]
        d["attachments"] = [a.to_dict() for a in self.attachments]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgendaItem":
        d = dict(d)
        kids = d.pop("children", []) or []
        atts = d.pop("attachments", []) or []
        item = cls(**_known(cls, d))
        item.children = [cls.from_dict(k) for k in kids]
        item.attachments = [Document(**_known(Document, a)) for a in atts]
        return item


@dataclass
class Meeting:
    """A single meeting on a single date."""

    # Stable identity. Once assigned, never changes -- see cache.assign_slug.
    slug: str
    body: str  # "Board of Commissioners"
    meeting_type: str | None  # "Work Session"
    title: str
    date: str  # ISO date, America/New_York, "2026-08-20"
    start_time: str | None  # "17:00" local, None if unknown
    start_iso: str | None  # full local ISO w/ offset, e.g. 2026-08-20T17:00:00-04:00
    location: str | None

    sources: list[str] = field(default_factory=list)  # which fetchers saw it
    civicclerk_event_id: int | None = None
    civicclerk_agenda_id: int | None = None
    county_meeting_id: int | None = None
    county_url: str | None = None
    portal_url: str | None = None

    documents: list[Document] = field(default_factory=list)
    items: list[AgendaItem] = field(default_factory=list)

    agenda_published: bool = False
    # Why there is no agenda, when there isn't one. Shown to readers verbatim.
    agenda_status_note: str | None = None
    # True when items were read out of a PDF rather than a structured API. The
    # page must say so: numbering is as printed, nesting is lost, and anything
    # the extractor could not read is simply absent.
    agenda_derived_from_pdf: bool = False

    fetched_at: str | None = None  # ISO8601 with timezone
    content_hash: str | None = None

    def all_items(self):
        for i in self.items:
            yield from i.walk()

    def to_dict(self) -> dict[str, Any]:
        return {
            **{
                k: v
                for k, v in asdict(self).items()
                if k not in ("documents", "items")
            },
            "documents": [d.to_dict() for d in self.documents],
            "items": [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Meeting":
        d = dict(d)
        docs = d.pop("documents", []) or []
        items = d.pop("items", []) or []
        m = cls(**_known(cls, d))
        m.documents = [Document(**_known(Document, x)) for x in docs]
        m.items = [AgendaItem.from_dict(x) for x in items]
        return m
