"""Command line entry point.

    uv run foco fetch          pull both sources into data/meetings.json
    uv run foco show           print what is in the cache
    uv run foco show --slug X  print one meeting in full
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import requests

from . import boe, cache, civicclerk, county, cumming, pdftext
from .config import DATA_DIR, load_config
from .http import PoliteSession, RobotsDenied


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


# Structural failures: the vendor changed their markup and our parser no
# longer understands it. These must stop the build -- publishing a silently
# shrunken site is exactly the rot this project is most exposed to.
PARSE_ERRORS = (
    boe.ParseError, civicclerk.ParseError, county.ParseError, cumming.ParseError,
)
# Transport failures: the far end is down, slow, or rate-limiting. These are
# transient and must NOT discard the sources that did succeed.
TRANSPORT_ERRORS = (RuntimeError, requests.RequestException)


def _fetch_source(name: str, enabled: bool, fn, failures: list) -> list:
    """Run one source. Returns its meetings, or [] and records why not."""
    if not enabled:
        return []
    try:
        meetings = fn()
    except RobotsDenied as exc:
        print(f"{name + ':':13}SKIPPED -- {exc}", file=sys.stderr)
        failures.append((name, "robots", str(exc)))
        return []
    except PARSE_ERRORS as exc:
        print(f"{name + ':':13}PARSE FAILURE -- {exc}", file=sys.stderr)
        failures.append((name, "parse", str(exc)))
        return []
    except TRANSPORT_ERRORS as exc:
        # Cumming's site returned HTTP 500 on every page for a stretch; a dead
        # source should degrade this run, not sink it.
        print(f"{name + ':':13}UNREACHABLE -- {exc}", file=sys.stderr)
        failures.append((name, "transport", str(exc)))
        return []
    print(f"{name + ':':13}{len(meetings)} meetings", file=sys.stderr)
    return meetings


def cmd_fetch(args) -> int:
    cfg = load_config()
    session = PoliteSession(cfg)
    data_dir = Path(args.data_dir)
    failures: list[tuple[str, str, str]] = []

    county_only = args.only_county
    cc_only = args.only_civicclerk
    secondary = not (county_only or cc_only)

    county_meetings = _fetch_source(
        "county", cfg.sources["county"]["enabled"] and not cc_only,
        lambda: county.fetch_all(session, cfg), failures)
    cc_meetings = _fetch_source(
        "civicclerk", cfg.sources["civicclerk"]["enabled"] and not county_only,
        lambda: civicclerk.fetch_all(session, cfg), failures)
    boe_meetings = _fetch_source(
        "boe", cfg.sources["boe"]["enabled"] and secondary,
        lambda: boe.fetch_all(session, cfg), failures)
    cumming_meetings = _fetch_source(
        "cumming", bool(cfg.sources.get("cumming", {}).get("enabled")) and secondary,
        lambda: cumming.fetch_all(session, cfg), failures)

    merged = cache.merge(
        county_meetings + boe_meetings + cumming_meetings, cc_meetings
    )

    if not merged:
        print("\nEVERY SOURCE FAILED -- refusing to touch the cache.",
              file=sys.stderr)
        return 1

    if not args.no_pdf:
        n = pdftext.enrich(merged, session, cfg, data_dir)
        print(f"pdf:         {n} meetings gained items from their agenda PDF",
              file=sys.stderr)

    ledger = cache.load_ledger(data_dir)
    previous = cache.load(data_dir)
    cache.assign_slugs(merged, ledger)
    # Stamp only what we actually fetched; archived meetings keep their original
    # fetched_at so the page never overstates how fresh it is.
    cache.stamp(merged, cfg)

    kept = cache.archive(previous, merged)
    carried = len(kept) - len(merged)

    changes = cache.diff(previous, kept)
    cache.save_ledger(data_dir, ledger)
    path = cache.save(kept, data_dir)

    print(f"merged:      {len(merged)} fetched"
          + (f", {carried} carried forward from earlier runs" if carried > 0 else "")
          + f" -> {path}", file=sys.stderr)
    if previous:
        print(
            f"changes:     +{len(changes['added'])} new, "
            f"~{len(changes['changed'])} changed, "
            f"-{len(changes['removed'])} gone",
            file=sys.stderr,
        )
        # Only record once there is a previous run to compare against, so the
        # first ever fetch does not announce every meeting as "new".
        fresh = cache.append_changelog(data_dir, changes, merged[0].fetched_at)
        if fresh:
            print(f"changelog:   {len(fresh)} entries appended", file=sys.stderr)

    if failures:
        print("\nSOURCES NOT FULLY FETCHED THIS RUN:", file=sys.stderr)
        for name, kind, detail in failures:
            print(f"  {name:12} {kind:10} {detail[:120]}", file=sys.stderr)
        print("  Meetings already known from these sources were carried "
              "forward from the previous run.", file=sys.stderr)

    # A parse failure means a vendor changed their markup and our reading of it
    # is now wrong. Stop, so a human looks, rather than quietly publishing less.
    parse_failures = [f for f in failures if f[1] == "parse"]
    if parse_failures and not args.allow_parse_failures:
        print(f"\n{len(parse_failures)} parser(s) failed. Exiting non-zero so "
              "this run does not publish.", file=sys.stderr)
        return 1
    return 0


def _print_meeting(m, full: bool) -> None:
    when = m.start_iso or f"{m.date} (time not published)"
    print(f"\n{'=' * 78}")
    print(f"{m.date}  {m.body}" + (f" — {m.meeting_type}" if m.meeting_type else ""))
    print(f"  slug:     {m.slug}")
    print(f"  when:     {when}")
    print(f"  where:    {m.location or '(not published)'}")
    print(f"  sources:  {', '.join(m.sources)}")
    if m.civicclerk_event_id:
        print(f"  civicclerk: event={m.civicclerk_event_id} agenda={m.civicclerk_agenda_id}")
    if m.agenda_status_note:
        print(f"  note:     {m.agenda_status_note}")

    if m.documents:
        print("  documents:")
        for d in m.documents:
            tag = "fetchable" if d.machine_readable else "JS-only "
            print(f"    [{tag}] {d.kind:<8} {d.label[:52]}")
            print(f"                {d.url}")

    items = list(m.all_items())
    if not items:
        return
    consent = sum(1 for i in items if i.under_consent)
    big = sum(1 for i in items if i.large_dollar)
    longterm = sum(1 for i in items if i.long_term)
    tagged = sum(1 for i in items if i.topics)
    print(f"  items:    {len(items)} total | {consent} under consent | "
          f"{big} over threshold | {longterm} long-term | {tagged} topic-tagged")

    if not full:
        return

    def show(item, depth=0):
        pad = "    " + "  " * depth
        num = item.outline_path or item.number or "(unnumbered)"
        marks = []
        if item.under_consent:
            marks.append("CONSENT")
        if item.large_dollar:
            marks.append(f"${max(item.dollar_amounts):,}")
        if item.long_term:
            marks.append("LONG-TERM")
        if item.topics:
            marks.append("/".join(item.topics))
        suffix = ("  <<" + " | ".join(marks) + ">>") if marks else ""
        print(f"{pad}{num:<12} {item.title[:96]}{suffix}")
        for c in item.children:
            show(c, depth + 1)

    print()
    for item in m.items:
        show(item)


def cmd_show(args) -> int:
    data_dir = Path(args.data_dir)
    meetings = cache.load(data_dir)
    if not meetings:
        print("cache is empty -- run `uv run foco fetch` first", file=sys.stderr)
        return 1

    if args.slug:
        picked = [m for m in meetings if m.slug == args.slug]
        if not picked:
            print(f"no meeting with slug {args.slug!r}", file=sys.stderr)
            return 1
        for m in picked:
            _print_meeting(m, full=True)
        return 0

    if args.date:
        meetings = [m for m in meetings if m.date == args.date]
    if args.body:
        needle = args.body.lower()
        meetings = [m for m in meetings if needle in m.body.lower()]
    if args.with_agenda:
        meetings = [m for m in meetings if list(m.all_items())]

    for m in meetings:
        _print_meeting(m, full=args.full)

    with_items = [m for m in meetings if list(m.all_items())]
    print(f"\n{'=' * 78}")
    print(f"{len(meetings)} meetings, {len(with_items)} with a published agenda")
    return 0


def cmd_build(args) -> int:
    """Render the static site: meeting pages, index, topics, sitemap, RSS, llms.txt."""
    import json as _json
    import shutil

    from . import render, sitegen

    cfg = load_config()
    data_dir = Path(args.data_dir)
    meetings = cache.load(data_dir)
    if not meetings:
        print("cache is empty -- run `uv run foco fetch` first", file=sys.stderr)
        return 1

    single = bool(args.slug)
    if single:
        selected = [m for m in meetings if m.slug == args.slug]
        if not selected:
            print(f"no meeting with slug {args.slug!r}", file=sys.stderr)
            return 1
    else:
        selected = meetings

    out = Path(args.out)
    (out / "assets").mkdir(parents=True, exist_ok=True)
    shutil.copy(render.ASSET_DIR / "style.css", out / "assets" / "style.css")

    # Topic chips only link out when this build actually produces topic pages.
    link_topics = args.link_topics or not single

    for m in selected:
        page_dir = out / "meetings" / m.slug
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(
            render.render_meeting(
                m, cfg, site_base=args.site_base, link_topics=link_topics
            ),
            encoding="utf-8",
        )
        # A machine-readable twin of every page, for agents that would rather
        # parse JSON than HTML.
        (page_dir / "index.json").write_text(
            _json.dumps(m.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    print(f"{len(selected)} meeting pages", file=sys.stderr)
    if single:
        print("single-page build: skipping index, topics, sitemap, RSS, llms.txt",
              file=sys.stderr)
        return 0

    (out / "index.html").write_text(
        sitegen.render_index(meetings, cfg, site_base=args.site_base), encoding="utf-8"
    )

    for key in cfg.topic_clusters():
        topic_dir = out / "topics" / key
        topic_dir.mkdir(parents=True, exist_ok=True)
        (topic_dir / "index.html").write_text(
            sitegen.render_topic(key, meetings, cfg, site_base=args.site_base),
            encoding="utf-8",
        )
    print(f"{len(cfg.topic_clusters())} topic pages", file=sys.stderr)

    # Whole-dataset download, for anyone who would rather not crawl page by page.
    (out / "meetings.json").write_text(
        _json.dumps([m.to_dict() for m in meetings], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if args.site_base:
        (out / "sitemap.xml").write_text(
            sitegen.render_sitemap(meetings, cfg, site_base=args.site_base),
            encoding="utf-8",
        )
        (out / "robots.txt").write_text(
            sitegen.render_robots(args.site_base), encoding="utf-8"
        )
        (out / "rss.xml").write_text(
            sitegen.render_rss(cache.load_changelog(data_dir), cfg,
                               site_base=args.site_base),
            encoding="utf-8",
        )
        (out / "llms.txt").write_text(
            sitegen.render_llms_txt(meetings, cfg, site_base=args.site_base),
            encoding="utf-8",
        )
        print("sitemap.xml, robots.txt, rss.xml, llms.txt", file=sys.stderr)
    else:
        print("no --site-base: skipping sitemap.xml, robots.txt, rss.xml, llms.txt "
              "(they need absolute URLs)", file=sys.stderr)

    print(f"site written to {out}", file=sys.stderr)
    return 0


def cmd_report(args) -> int:
    """Print what changed on the most recent fetch. Cron-friendly."""
    entries = cache.load_changelog(Path(args.data_dir))
    if not entries:
        print("No changes recorded.")
        return 0

    latest = entries[0]["at"]
    if args.latest_only:
        entries = [e for e in entries if e["at"] == latest]
    else:
        entries = entries[: args.limit]

    if not entries:
        print("No changes recorded.")
        return 0

    print(f"{len(entries)} change(s), most recent run {latest}\n")
    for e in entries:
        marker = "NEW    " if e["kind"] == "new" else "CHANGED"
        print(f"{marker}  {e['date']}  {e['title']}")
        print(f"         {e['summary']}")
        print(f"         {e['slug']}")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="foco", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="pull both sources into the cache")
    f.add_argument("--only-county", action="store_true")
    f.add_argument("--only-civicclerk", action="store_true")
    f.add_argument("--no-pdf", action="store_true",
                   help="skip extracting agenda text from PDFs")
    f.add_argument("--allow-parse-failures", action="store_true",
                   help="exit 0 even if a parser failed (for local debugging; "
                        "never use in CI)")
    f.set_defaults(func=cmd_fetch)

    s = sub.add_parser("show", help="print the cache")
    s.add_argument("--slug")
    s.add_argument("--date")
    s.add_argument("--body")
    s.add_argument("--full", action="store_true", help="print every agenda item")
    s.add_argument("--with-agenda", action="store_true")
    s.set_defaults(func=cmd_show)

    b = sub.add_parser("build", help="render meeting pages to a static directory")
    b.add_argument("--slug", help="render only this meeting")
    b.add_argument("--out", default="site")
    b.add_argument("--site-base", default="", help="absolute base URL for canonical links")
    b.add_argument(
        "--link-topics", action="store_true",
        help="make topic chips clickable; only valid once topic pages are generated",
    )
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("report", help="print what changed on the last fetch")
    r.add_argument("--latest-only", action="store_true",
                   help="only changes from the most recent run")
    r.add_argument("--limit", type=int, default=50)
    r.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
