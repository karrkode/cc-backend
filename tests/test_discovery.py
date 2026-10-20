"""CC-70: event discovery: polite fetching, reading as little as possible, the holding list."""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.discovery import fetch as web
from app.discovery import run as runner
from app.discovery.fetch import Page, parse_robots
from app.discovery.pagetext import page_text
from app.discovery.readers import FoundEvent, PageReading, Performance
from app.discovery.rules import apply_rules
from app.models import DiscoveredItem, DiscoveryCheck, DiscoverySource, SourceKind, Venue

from .conftest import auth

TODAY = date(2026, 10, 20)


# ---- polite fetching ----------------------------------------------------------------

def test_robots_rules_ai_opt_out_and_crawl_delay():
    r = parse_robots("User-agent: ClaudeBot\nUser-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /search\nAllow: /search/ok\nCrawl-delay: 10\n")
    assert r.ai_opt_out and r.crawl_delay == 10
    assert r.allows("/shows") and not r.allows("/search/x") and r.allows("/search/ok/1")
    assert not parse_robots("User-agent: *\nDisallow:\n").ai_opt_out
    assert not parse_robots("User-agent: *\nDisallow: /\n").allows("/anything")


def test_page_text_keeps_links_and_drops_navigation():
    html = """<html><head><title>x</title><script>var a</script></head><body><nav>Menu <a href="/a">A</a></nav>
      <h2>Hamlet</h2><p>Nov 1 – Dec 5 <a href="/shows/hamlet">Details</a> <a href="https://facebook.com/x">FB</a></p>
      <footer>Footer</footer></body></html>"""
    text, cut = page_text(html, "https://www.example.org/season/")
    assert "Hamlet" in text and "<https://www.example.org/shows/hamlet>" in text
    assert "Menu" not in text and "Footer" not in text and "facebook" not in text and "var a" not in text and not cut


# ---- what to leave out ------------------------------------------------------------------

def test_rules_leave_out_classes_rentals_members_and_repeats():
    ev = lambda title, kind, days=(), **kw: FoundEvent(title=title, kind=kind, performances=[Performance(date=d) for d in days], **kw)
    found = [ev("Improv 101", "class"), ev("Gala", "rental"), ev("Member preview", "members_only"),
             ev("Family Day", "program", recurring=True),
             ev("Highlights tour", "tour", ["2026-11-01", "2026-11-05", "2026-11-09"]),  # 3 dates in 2 weeks
             ev("Curator talk", "talk", ["2026-11-01"]), ev("Hamlet", "production", ["2026-11-01", "2026-11-02", "2026-11-03"]),
             ev("Teen Studio Workshop: In the Dark", "program")]  # a class, though the source didn't say
    reasons = {e.title: why for e, why in apply_rules(found, "museum")}
    assert reasons["Improv 101"] and reasons["Gala"] and reasons["Member preview"] and reasons["Family Day"]
    assert reasons["Highlights tour"] == "repeats on 3 or more dates within two weeks"
    assert reasons["Curator talk"] == "" and reasons["Hamlet"] == "" and reasons["Teen Studio Workshop: In the Dark"]
    assert dict((e.title, w) for e, w in apply_rules(found[4:5], "theater"))["Highlights tour"] == ""  # only museums' programs


def test_rules_leave_out_permanent_galleries():
    found = [FoundEvent(title="U-505 Submarine", kind="exhibition", open_run=True),
             FoundEvent(title="Coal Mine", kind="permanent"),
             FoundEvent(title="Pokémon Fossil Museum", kind="exhibition", end_date="2027-04-11"),
             FoundEvent(title="New Wing", kind="exhibition", open_run=True, begin_date="2026-12-01")]
    reasons = {e.title: why for e, why in apply_rules(found, "museum")}
    assert reasons == {"U-505 Submarine": "a permanent gallery", "Coal Mine": "a permanent gallery",
                       "Pokémon Fossil Museum": "", "New Wing": ""}
    assert {w for _, w in apply_rules(found[:1], "theater")} == {""}  # an open-run show isn't a gallery


# ---- checking sources ---------------------------------------------------------------------

class FakeClaude:
    def __init__(self, events):
        self.events, self.calls = events, 0
        self.messages = SimpleNamespace(parse=self.parse)

    def parse(self, **kw):
        self.calls += 1
        assert kw["model"] == "claude-haiku-5-5"
        return SimpleNamespace(stop_reason="end_turn", parsed_output=PageReading(events=list(self.events)),
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


@pytest.fixture
def pages(monkeypatch):
    """Serve fake pages: url -> html, or an exception to raise. Counts requests."""
    served, calls = {}, []

    def fake_fetch(url, etag="", last_modified="", ai=True):
        calls.append((url, etag))
        got = served[url]
        if isinstance(got, Exception):
            raise got
        if etag and etag == "v1" and got.get("same_etag"):
            return Page(url=url, status=304, etag=etag)
        return Page(url=url, status=200, text=got["html"], etag="v1")
    monkeypatch.setattr(web, "fetch", fake_fetch)
    return served, calls


def make_source(db, **kw):
    venue = Venue(name="Goodman Theatre", neighborhood="Loop")
    db.add(venue); db.flush()
    s = DiscoverySource(name="Goodman", kind=SourceKind.theater, pages=["https://goodman.example/season"], reader="claude",
                        venue_id=venue.id, venue_name="Goodman Theatre", neighborhood="Loop", every_days=30, page_state={},
                        venue_hours={}, unchanged_streak=0, last_status="", last_error="", **kw)
    db.add(s); db.commit()
    return s


SEASON = [FoundEvent(title="Hamlet", url="https://goodman.example/shows/hamlet", kind="production", category="Theater",
                     begin_date="2027-01-10", end_date="2027-02-14"),
          FoundEvent(title="Playwriting Workshop", kind="class")]


def test_first_check_finds_events_then_unchanged_pages_cost_nothing(db, pages):
    served, calls = pages
    served["https://goodman.example/season"] = {"html": "<p>Hamlet</p>"}
    s, claude = make_source(db), FakeClaude(SEASON)
    results = runner.run(db, claude, TODAY)
    out = results[0][1]
    assert (out.status, out.new, out.skipped, out.pages_read, claude.calls) == ("ok", 1, 1, 1, 1)
    items = {i.title: i for i in db.scalars(select(DiscoveredItem))}
    assert items["Hamlet"].status == "new" and items["Hamlet"].begin_date == date(2027, 1, 10)
    assert items["Playwriting Workshop"].status == "skipped"
    assert s.next_check_on == TODAY + timedelta(days=30) and s.last_status == "ok"

    # not due yet: nothing is fetched
    calls.clear()
    assert runner.run(db, claude, TODAY + timedelta(days=5)) == [] and calls == []
    # due, same text: fetched, but not sent to the model
    later = TODAY + timedelta(days=30)
    out = runner.run(db, claude, later)[0][1]
    assert (out.status, out.pages_read, claude.calls) == ("unchanged", 0, 1)
    # the site says 304 Not Modified: unchanged too
    served["https://goodman.example/season"]["same_etag"] = True
    s.next_check_on = None; db.commit()
    assert runner.run(db, claude, later)[0][1].status == "unchanged" and claude.calls == 1


def test_back_off_after_three_unchanged_checks(db, pages):
    served, _ = pages
    served["https://goodman.example/season"] = {"html": "<p>Hamlet</p>"}
    s, claude = make_source(db), FakeClaude(SEASON)
    day = TODAY
    gaps = []
    for _ in range(5):
        runner.run(db, claude, day)
        gaps.append((s.next_check_on - day).days)
        day = s.next_check_on
    assert gaps == [30, 30, 30, 45, 45]  # 1 changed, then unchanged x2 at 30, stretched after the 3rd
    served["https://goodman.example/season"] = {"html": "<p>Hamlet and a new show</p>"}
    runner.run(db, claude, day)
    assert (s.next_check_on - day).days == 30 and s.unchanged_streak == 0  # back to normal


def test_blocked_and_failing_sources_are_logged(db, pages):
    served, _ = pages
    s = make_source(db)
    served["https://goodman.example/season"] = web.Blocked("the site refused the request (HTTP 403)")
    out = runner.run(db, FakeClaude([]), TODAY)[0][1]
    assert out.status == "blocked" and "403" in s.last_error and s.next_check_on == TODAY + timedelta(days=30)
    served["https://goodman.example/season"] = web.FetchError("HTTP 500")
    s.next_check_on = None; db.commit()
    runner.run(db, FakeClaude([]), TODAY)
    assert s.last_status == "error" and s.next_check_on == TODAY + timedelta(days=1)  # try again tomorrow
    checks = db.scalars(select(DiscoveryCheck)).all()
    assert [c.status for c in checks] == ["blocked", "error"]


def test_no_api_key_reports_it_instead_of_reading(db, pages):
    served, _ = pages
    served["https://goodman.example/season"] = {"html": "<p>Hamlet</p>"}
    s = make_source(db)
    out = runner.run(db, None, TODAY)[0][1]
    assert out.status == "error" and "API key" in s.last_error and db.scalar(select(DiscoveredItem)) is None


def test_already_listed_events_and_proposed_updates(db, pages, make_event):
    served, _ = pages
    served["https://goodman.example/season"] = {"html": "<p>v1</p>"}
    s = make_source(db)
    listed = make_event("Hamlet", venue_name="Goodman Theatre", neighborhood="Loop")  # from the sheet, no price yet
    listed.cost_text, listed.min_price, listed.begin_date, listed.end_date = "", None, date(2027, 1, 10), date(2027, 2, 14)
    db.commit()
    runner.run(db, FakeClaude(SEASON[:1]), TODAY)
    item = db.scalar(select(DiscoveredItem))
    assert item.status == "listed" and item.event_id == listed.id  # not proposed twice
    # later the theater adds prices: proposed as an update, the event itself untouched
    served["https://goodman.example/season"] = {"html": "<p>v2</p>"}
    s.next_check_on = None; db.commit()
    priced = SEASON[0].model_copy(update={"cost_text": "$35–$90", "min_price": 35})
    out = runner.run(db, FakeClaude([priced]), TODAY)[0][1]
    db.refresh(item); db.refresh(listed)
    assert out.updates == 1 and item.status == "update"
    assert item.changes == {"cost_text": ["", "$35–$90"], "min_price": [None, 35.0]}
    assert listed.cost_text == "" and listed.min_price is None


def test_pre_opening_checks_read_just_the_event_page(db, pages):
    served, calls = pages
    served["https://goodman.example/season"] = {"html": "<p>Hamlet</p>"}
    served["https://goodman.example/shows/hamlet"] = {"html": "<p>Hamlet: tickets $40</p>"}
    s = make_source(db)
    runner.run(db, FakeClaude(SEASON[:1]), TODAY)
    item = db.scalar(select(DiscoveredItem))
    opening = item.begin_date
    # 70 days out: nothing; 59 days out: the 60-day check; 44 days out: the 45-day check; then no more
    for days_before, expect in ((70, []), (59, [60]), (44, [45, 60]), (30, [45, 60])):
        calls.clear()
        runner.run(db, FakeClaude(SEASON[:1]), opening - timedelta(days=days_before))
        db.refresh(item)
        assert item.detail_checks == expect, days_before
        assert (("https://goodman.example/shows/hamlet", "") in calls) == (days_before in (59, 44))


def test_admin_sources_endpoints(client, admin_token, user_token, db, pages):
    s = make_source(db)
    h = auth(admin_token)
    r = client.get("/admin/discovery/sources", headers=h)
    assert r.status_code == 200 and r.json()["sources"][0]["name"] == "Goodman" and r.json()["cost_30d_usd"] == 0
    r = client.patch(f"/admin/discovery/sources/{s.id}", json={"every_days": 14, "active": False}, headers=h)
    assert (r.json()["every_days"], r.json()["active"]) == (14, False)
    assert client.patch(f"/admin/discovery/sources/{s.id}", json={"pages": ["javascript:alert(1)"]}, headers=h).status_code == 422
    s.next_check_on, s.unchanged_streak = TODAY + timedelta(days=40), 4; db.commit()
    r = client.post(f"/admin/discovery/sources/{s.id}/check", headers=h)
    assert r.json()["next_check_on"] is None and r.json()["unchanged_streak"] == 0
    assert client.get("/admin/discovery/sources", headers=auth(user_token)).status_code == 403
    assert client.post(f"/admin/discovery/sources/{s.id}/check").status_code == 401


def test_matches_the_same_venue_entered_twice(db, pages, make_event):
    """'The Art Institute of Chicago' and 'Art Institute of Chicago' are one place."""
    served, _ = pages
    served["https://goodman.example/season"] = {"html": "<p>x</p>"}
    s = make_source(db)
    listed = make_event("Hamlet", venue_name="The Goodman Theatre", neighborhood="Loop")
    listed.begin_date, listed.end_date = date(2027, 1, 10), date(2027, 2, 14); db.commit()
    other = make_event("Hamlet", venue_name="Elsewhere Hall", neighborhood="Loop")  # same title, another venue
    runner.run(db, FakeClaude([SEASON[0].model_copy(update={"title": "Hamlet!"})]), TODAY)
    item = db.scalar(select(DiscoveredItem))
    assert item.status == "listed" and item.event_id == listed.id != other.id


def test_same_event_with_and_without_a_link_is_one_item(db):
    from app.discovery.store import record
    s = make_source(db)
    assert record(db, s, FoundEvent(title="Anne Frank: The Exhibition", kind="exhibition"), "") == "new"
    assert record(db, s, FoundEvent(title="Anne Frank The Exhibition", url="https://goodman.example/anne-frank",
                                    kind="exhibition"), "") == "seen"
    assert record(db, s, FoundEvent(title="ANNE FRANK the exhibition", kind="exhibition"), "") == "seen"
    items = db.scalars(select(DiscoveredItem)).all()
    assert [(i.key, i.url) for i in items] == [("https://goodman.example/anne-frank", "https://goodman.example/anne-frank")]
    # two seasons of the same show, each with its own page, stay two items
    record(db, s, FoundEvent(title="The Magic Parlour", url="https://goodman.example/parlour-25", kind="production"), "")
    record(db, s, FoundEvent(title="The Magic Parlour", url="https://goodman.example/parlour-26", kind="production"), "")
    assert db.scalar(select(func.count()).select_from(DiscoveredItem)) == 3


def test_a_closer_look_can_leave_out_a_new_item(db):
    from app.discovery.store import record
    s = make_source(db)
    record(db, s, FoundEvent(title="Farmers Market", url="https://goodman.example/market", kind="program"), "")
    assert record(db, s, FoundEvent(title="Farmers Market", url="https://goodman.example/market", kind="recurring"),
                  "a recurring generic offering") == "skipped"
    item = db.scalar(select(DiscoveredItem))
    assert (item.status, item.skip_reason) == ("skipped", "a recurring generic offering")
