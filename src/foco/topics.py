"""Flagging: dollar figures, long terms, topic clusters, consent detection.

Everything configurable lives in config/topics.toml. This module only implements
the matching rules.
"""

from __future__ import annotations

import re
from html import unescape

from .config import Config

# "$254,200.00" / "$11,333" / "$239.4 million" / "$1.2M"
_MONEY = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(million|billion|thousand|[MBK])?\b",
    re.IGNORECASE,
)
_SCALE = {
    "thousand": 1_000, "k": 1_000,
    "million": 1_000_000, "m": 1_000_000,
    "billion": 1_000_000_000, "b": 1_000_000_000,
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(raw: str | None) -> str:
    """Strip the raw HTML fragments CivicClerk embeds in item names.

    Some agenda item names arrive as e.g.
    '<p style="margin-left:0in;" data-pasted="true">Board approval of ...'
    Escaping that would show markup to readers; stripping is correct.
    """
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = unescape(text)
    # CivicClerk sprinkles zero-width and non-breaking spaces through pasted text.
    text = text.replace("​", "").replace("\xa0", " ").replace("⁠", "")
    return _WS_RE.sub(" ", text).strip()


def parse_dollar_amounts(text: str) -> list[int]:
    """Return whole-dollar amounts mentioned in `text`, largest first."""
    out: list[int] = []
    for m in _MONEY.finditer(text or ""):
        raw, scale = m.group(1), (m.group(2) or "").lower()
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if scale:
            value *= _SCALE.get(scale, 1)
        out.append(int(round(value)))
    return sorted(set(out), reverse=True)


def detect_long_term(text: str, cfg: Config) -> str | None:
    """Return the matched phrase if this looks like a >1yr commitment."""
    for pattern in cfg.term_patterns:
        m = re.search(pattern, text or "", re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def is_consent_marker(name: str, cfg: Config) -> bool:
    low = (name or "").lower()
    return any(marker in low for marker in cfg.consent_markers)


def is_consent_reset(name: str, cfg: Config) -> bool:
    low = (name or "").strip().lower()
    return any(low.startswith(reset) for reset in cfg.consent_resets)


def tag_topics(text: str, cfg: Config) -> list[str]:
    """Return topic keys whose keyword/regex cluster matches `text`."""
    low = (text or "").lower()
    hits: list[str] = []
    for key, cluster in cfg.topic_clusters().items():
        matched = any(kw.lower() in low for kw in cluster.get("keywords", []))
        if not matched:
            matched = any(
                re.search(rx, text or "", re.IGNORECASE)
                for rx in cluster.get("regex", [])
            )
        if matched:
            hits.append(key)
    return hits


def apply_flags(item, cfg: Config) -> None:
    """Populate dollar/term/topic flags on one AgendaItem, in place."""
    text = item.title or ""
    if item.fiscal_impact:
        text = f"{text} {item.fiscal_impact}"
    item.dollar_amounts = parse_dollar_amounts(text)
    item.large_dollar = any(
        amount >= cfg.large_dollar_threshold for amount in item.dollar_amounts
    )
    reason = detect_long_term(text, cfg)
    item.long_term = reason is not None
    item.long_term_reason = reason
    item.topics = tag_topics(text, cfg)
