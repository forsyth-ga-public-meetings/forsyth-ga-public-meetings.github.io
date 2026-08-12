"""One broken source must not sink the run.

Regression tests for the first production failure: cityofcumming.net returned
HTTP 500 on every page, the retry gave up and raised, and the whole fetch
aborted -- discarding 55 county, 8 CivicClerk and 6 Board of Education
meetings that had already been fetched successfully.

The rule these pin down:
  transport failure (host down, timeout)  -> degrade, keep going, publish
  parse failure (markup changed)          -> stop, so a human looks
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from foco import cli, county, pdftext
from foco.config import load_config
from foco.model import Document, Meeting

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _boom(exc):
    def raise_it():
        raise exc
    return raise_it


def _meeting(date="2026-08-20"):
    return Meeting(
        slug="", body="Board of Commissioners", meeting_type="Work Session",
        title="t", date=date, start_time="14:00",
        start_iso=f"{date}T14:00:00-04:00", location=None, sources=["county"],
    )


# ---------------------------------------------------------------------------
# _fetch_source classification
# ---------------------------------------------------------------------------

def test_transport_failure_is_recorded_not_raised():
    """This is the exact shape of the production failure."""
    failures: list = []
    got = cli._fetch_source(
        "cumming", True,
        _boom(RuntimeError(
            "giving up on https://www.cityofcumming.net/city-council-agendas")),
        failures)
    assert got == []
    assert failures == [("cumming", "transport", failures[0][2])]
    assert failures[0][1] == "transport"


def test_requests_exception_counts_as_transport():
    failures: list = []
    cli._fetch_source("county", True,
                      _boom(requests.ConnectionError("dns failure")), failures)
    assert failures[0][1] == "transport"


def test_parse_failure_is_classified_separately():
    failures: list = []
    cli._fetch_source("county", True,
                      _boom(county.ParseError("markup changed")), failures)
    assert failures[0][1] == "parse"


def test_disabled_source_is_silently_skipped():
    failures: list = []
    assert cli._fetch_source("cumming", False, _boom(RuntimeError("x")),
                             failures) == []
    assert failures == []


def test_successful_source_records_no_failure():
    failures: list = []
    got = cli._fetch_source("county", True, lambda: [_meeting()], failures)
    assert len(got) == 1
    assert failures == []


# ---------------------------------------------------------------------------
# end-to-end: a dead source degrades, a broken parser stops the build
# ---------------------------------------------------------------------------

def _run_fetch(tmp_path, monkeypatch, *, cumming_exc=None, county_exc=None):
    from foco import boe, civicclerk, cumming as cumming_mod

    monkeypatch.setattr(county, "fetch_all",
                        lambda s, c: (_ for _ in ()).throw(county_exc)
                        if county_exc else [_meeting("2026-08-20")])
    monkeypatch.setattr(civicclerk, "fetch_all", lambda s, c: [])
    monkeypatch.setattr(boe, "fetch_all", lambda s, c: [_meeting("2026-08-18")])
    monkeypatch.setattr(cumming_mod, "fetch_all",
                        lambda s, c: (_ for _ in ()).throw(cumming_exc)
                        if cumming_exc else [])
    monkeypatch.setattr(pdftext, "enrich", lambda *a, **k: 0)

    return cli.main(["--data-dir", str(tmp_path), "fetch"])


def test_dead_source_still_publishes_the_others(tmp_path, monkeypatch):
    """The production case: Cumming down, everything else fine -> exit 0."""
    rc = _run_fetch(tmp_path, monkeypatch,
                    cumming_exc=RuntimeError("giving up on cityofcumming.net"))
    assert rc == 0

    data = json.loads((tmp_path / "meetings.json").read_text("utf-8"))
    assert len(data) == 2, "county and BoE meetings must survive"


def test_broken_parser_stops_the_run(tmp_path, monkeypatch):
    rc = _run_fetch(tmp_path, monkeypatch,
                    county_exc=county.ParseError("no cards found"))
    assert rc == 1, "a markup change must not publish silently"


def test_parse_failure_can_be_overridden_for_local_debugging(tmp_path, monkeypatch):
    from foco import boe, civicclerk, cumming as cumming_mod

    monkeypatch.setattr(county, "fetch_all",
                        lambda s, c: (_ for _ in ()).throw(
                            county.ParseError("no cards")))
    monkeypatch.setattr(civicclerk, "fetch_all", lambda s, c: [])
    monkeypatch.setattr(boe, "fetch_all", lambda s, c: [_meeting()])
    monkeypatch.setattr(cumming_mod, "fetch_all", lambda s, c: [])
    monkeypatch.setattr(pdftext, "enrich", lambda *a, **k: 0)

    rc = cli.main(["--data-dir", str(tmp_path), "fetch", "--allow-parse-failures"])
    assert rc == 0


def test_every_source_failing_refuses_to_touch_the_cache(tmp_path, monkeypatch):
    from foco import boe, civicclerk, cumming as cumming_mod

    for mod in (county, civicclerk, boe, cumming_mod):
        monkeypatch.setattr(mod, "fetch_all",
                            lambda s, c: (_ for _ in ()).throw(
                                RuntimeError("host down")))
    monkeypatch.setattr(pdftext, "enrich", lambda *a, **k: 0)

    rc = cli.main(["--data-dir", str(tmp_path), "fetch"])
    assert rc == 1
    assert not (tmp_path / "meetings.json").exists(), \
        "an all-sources outage must not overwrite a good cache with nothing"


def test_existing_cache_survives_a_total_outage(tmp_path, monkeypatch):
    from foco import boe, cache, civicclerk, cumming as cumming_mod

    good = _meeting("2026-08-20")
    good.slug = "keeper"
    good.fetched_at = "2026-08-11T16:00:00-04:00"
    cache.save([good], tmp_path)

    for mod in (county, civicclerk, boe, cumming_mod):
        monkeypatch.setattr(mod, "fetch_all",
                            lambda s, c: (_ for _ in ()).throw(
                                RuntimeError("host down")))
    monkeypatch.setattr(pdftext, "enrich", lambda *a, **k: 0)

    assert cli.main(["--data-dir", str(tmp_path), "fetch"]) == 1
    data = json.loads((tmp_path / "meetings.json").read_text("utf-8"))
    assert [m["slug"] for m in data] == ["keeper"]


# ---------------------------------------------------------------------------
# PDF enrichment must survive an unreachable document host
# ---------------------------------------------------------------------------

def test_unreachable_pdf_does_not_sink_enrichment(cfg, tmp_path):
    m = _meeting()
    m.slug = "x"
    m.documents = [Document("Agenda PDF", "https://x.invalid/a.pdf",
                            "agenda", "county")]

    class DeadSession:
        def get(self, *a, **kw):
            raise RuntimeError("giving up on https://x.invalid/a.pdf")

    assert pdftext.enrich([m], DeadSession(), cfg, tmp_path) == 0
    assert m.items == []
    # The document link survives even though its text could not be read.
    assert len(m.documents) == 1
