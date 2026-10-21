"""CC-80: the monthly budget, event-page rules, and reading a page's own event data."""
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.discovery import run as runner
from app.discovery.budget import budget_state, month_start, set_monthly_budget
from app.discovery.jsonld import complete, events_in
from app.discovery.readers import FoundEvent, Performance
from app.discovery.recap import build_recap
from app.models import DiscoveredItem, DiscoveryCheck
from app.stats import today_in_chicago

from .conftest import auth
from .test_discovery import FakeClaude, TODAY, make_source, pages  # noqa: F401 - pages is a fixture

LD = """<html><body><script type="application/ld+json">{"@graph": [%s]}</script><p>Our season</p></body></html>"""


def ld_event(name, start, end, price):
    return ('{"@type": "TheaterEvent", "name": "%s", "url": "/shows/%s", "startDate": "%s", "endDate": "%s", '
            '"offers": {"@type": "Offer", "price": "%s"}}' % (name, name.lower(), start, end, price))


def spend(db, source, dollars):
    """Log a check costing about this much (all output tokens at $0.50 per million)."""
    db.add(DiscoveryCheck(source_id=source.id, status="ok", pages_read=1, input_tokens=0, output_tokens=int(dollars / 0.5 * 1e6)))
    db.commit()


def test_budget_states_and_settings(client, admin_token, db):
    s = make_source(db)
    assert budget_state(db)[0] == "ok"
    spend(db, s, 8.5)
    assert budget_state(db) == ("saving", 8.5, 10.0)
    h = auth(admin_token)
    r = client.put("/admin/discovery/budget", json={"usd": 20}, headers=h)
    assert r.status_code == 200 and (r.json()["budget_usd"], r.json()["budget_state"]) == (20, "ok")
    assert client.put("/admin/discovery/budget", json={"usd": 0}, headers=h).status_code == 422
    set_monthly_budget(db, 5); db.commit()
    assert budget_state(db)[0] == "paused"
    assert "budget used up" in build_recap(db).subject
    # a check from last month doesn't count
    db.query(DiscoveryCheck).update({DiscoveryCheck.checked_at: month_start(datetime.now(UTC)) - timedelta(minutes=1)}); db.commit()
    assert budget_state(db)[0] == "ok"


def test_over_budget_only_check_now_runs(client, admin_token, db, pages):  # noqa: F811
    served, calls = pages
    served["https://goodman.example/season"] = {"html": "<p>Hamlet</p>"}
    s = make_source(db)
    set_monthly_budget(db, 1); spend(db, s, 1.0)
    out = runner.run(db, FakeClaude([]), TODAY)
    assert out[0][1].status == "waiting" and not calls  # nothing was read
    assert client.post(f"/admin/discovery/sources/{s.id}/check", headers=auth(admin_token)).status_code == 200
    db.expire_all()
    out = runner.run(db, FakeClaude([]), TODAY)
    assert out[0][1].status == "ok" and calls  # Check now still runs
    db.refresh(s)
    assert s.check_requested is False
    assert out[-1][0] == "Event pages" and out[-1][1].status == "waiting"  # event pages wait


def test_a_pages_own_event_data_is_used_instead_of_claude(db, pages):  # noqa: F811
    served, _ = pages
    shows = ",".join([ld_event("Hamlet", "2027-01-10", "2027-02-14", "35"), ld_event("Macbeth", "2027-03-01", "2027-03-30", "40"),
                      ld_event("Cymbeline", "2027-04-01", "2027-04-30", "0")])
    served["https://goodman.example/season"] = {"html": LD % shows}
    s = make_source(db)
    claude = FakeClaude([])
    out = runner.run(db, claude, TODAY)[0][1]
    assert claude.calls == 0 and out.pages_read == 0 and out.new == 3
    items = {i.title: i for i in db.scalars(select(DiscoveredItem))}
    assert (items["Cymbeline"].cost_text, items["Hamlet"].url) == ("Free", "https://goodman.example/shows/hamlet")
    # too little data (two events, or no prices): Claude reads the page as before
    thin = LD % ld_event("Hamlet", "2027-01-10", "2027-02-14", "35").replace('"offers"', '"x"')
    served["https://goodman.example/season"] = {"html": thin.replace("Our season", "Our new season")}
    runner.run(db, claude, TODAY, force=True)
    assert claude.calls == 1


def test_thin_event_data_isnt_trusted():
    e = events_in(LD % '{"@type": "TheaterEvent", "name": "The Attic", "startDate": "2026-09-05"}', "https://x/", "theater")
    assert [x.title for x in e] == ["The Attic"] and not complete(e[0])


def test_event_pages_are_skipped_when_theres_nothing_to_learn(db, pages):  # noqa: F811
    served, calls = pages
    served["https://goodman.example/season"] = {"html": "<p>season</p>"}
    s = make_source(db)
    today = today_in_chicago()  # discovery's "found today" is the Chicago day, not the machine's (UTC on CI)
    soon = FoundEvent(title="Soon", url="https://goodman.example/soon", kind="production", category="Theater",
                      begin_date=(today + timedelta(days=5)).isoformat())
    known = FoundEvent(title="Known", url="https://goodman.example/known", kind="production", category="Theater",
                       begin_date=(today + timedelta(days=40)).isoformat(), cost_text="$30",
                       performances=[Performance(date=(today + timedelta(days=40)).isoformat(), time="19:30")])
    later = FoundEvent(title="Later", url="https://goodman.example/later", kind="production", category="Theater",
                       begin_date=(today + timedelta(days=40)).isoformat())
    served.update({e.url: {"html": "<p>x</p>"} for e in (soon, known, later)})
    runner.run(db, FakeClaude([soon, known, later]), today)
    assert not [u for u, _ in calls if u in (soon.url, known.url, later.url)]  # all found today: the list page was just read
    calls.clear()
    tomorrow = today + timedelta(days=1)
    db.query(DiscoveredItem).update({DiscoveredItem.first_seen_at: datetime.now(UTC) - timedelta(days=1)}); db.commit()
    runner.run(db, FakeClaude([later]), tomorrow)
    assert [u for u, _ in calls if u != "https://goodman.example/season"] == [later.url]  # not too soon, not already known
    marks = {i.title: i.detail_checks for i in db.scalars(select(DiscoveredItem))}
    assert marks["Soon"] == [45, 60] and marks["Known"] == [45, 60]  # never looked at again
