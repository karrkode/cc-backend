"""CC-80: the monthly budget, event-page rules, and reading a page's own event data."""
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.discovery import run as runner
from app import mail
from app.discovery.budget import alert_if_short, best_first, budget_state, month_start, set_monthly_budget
from app.discovery.jsonld import complete, events_in
from app.discovery.readers import FoundEvent, Performance
from app.discovery.recap import build_recap
from app.models import DiscoveredItem, DiscoveryCheck, DiscoverySource, Event, EventStatus, SourceKind, Venue
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


# ---- CC-211: the 80% alert, and the best sources first -----------------------------------------

def other_source(db, name, **kw):
    s = DiscoverySource(name=name, kind=SourceKind.theater, pages=[f"https://{name}.example/season"], **kw)
    db.add(s); db.commit()
    return s


def approve(db, source, n, status=EventStatus.approved):
    venue = Venue(name=f"{source.name} {status.value} hall")
    db.add(venue); db.flush()
    for i in range(n):
        e = Event(title=f"{source.name} {status.value} {i}", status=status, venue_id=venue.id, category="Theater")
        db.add(e); db.flush()
        db.add(DiscoveredItem(source_id=source.id, key=f"{status.value}{i}", title=e.title, status="approved", event_id=e.id))
    db.commit()


@pytest.fixture(autouse=True)
def empty_outbox():
    mail.OUTBOX.clear()  # only the client fixture clears it, and earlier tests here spend the budget too


def alerts():
    return [e for e in mail.OUTBOX if "budget" in e.subject]


def test_the_alert_goes_out_once_a_month_at_80_percent(db):
    s = make_source(db)
    spend(db, s, 7.9)
    assert not alert_if_short(db) and not alerts()  # under 80%
    spend(db, s, 0.1)
    assert alert_if_short(db)
    assert [e.to for e in alerts()] == ["info@chiculturecrew.com"]
    assert alerts()[0].subject == "Event discovery: over 80% of the budget spent"
    assert "$8.00 of the $10 monthly budget" in alerts()[0].text and "/#sources" in alerts()[0].text
    spend(db, s, 2.5)  # now used up: the recap and Check now rules cover the rest of the month
    assert not alert_if_short(db) and len(alerts()) == 1
    set_monthly_budget(db, 10.5); db.commit()  # a new budget is reached: admins hear again
    assert alert_if_short(db) and len(alerts()) == 2
    # next month starts again
    next_month = datetime.now(UTC) + timedelta(days=32)
    assert not alert_if_short(db, next_month)
    spend(db, s, 9)
    db.query(DiscoveryCheck).update({DiscoveryCheck.checked_at: next_month}); db.commit()
    assert alert_if_short(db, next_month) and len(alerts()) == 3


def test_going_straight_past_100_percent_still_alerts(db):
    spend(db, make_source(db), 11)
    assert alert_if_short(db)
    assert alerts()[0].subject == "Event discovery: budget used up" and "Only Check now runs" in alerts()[0].text


def test_a_failed_alert_is_tried_again_and_doesnt_stop_discovery(db, monkeypatch):
    spend(db, make_source(db), 8)

    def broken(email, strict=False):
        raise RuntimeError("SES is down")
    monkeypatch.setattr("app.discovery.budget.send", broken)
    assert not alert_if_short(db)
    monkeypatch.undo()
    assert alert_if_short(db) and len(alerts()) == 1


def test_a_run_sends_the_alert(db, pages):  # noqa: F811
    s = make_source(db)
    s.next_check_on = TODAY + timedelta(days=5)
    spend(db, s, 8.5)
    runner.run(db, FakeClaude([]), TODAY)
    assert len(alerts()) == 1


def test_the_recap_says_when_over_80_percent(db):
    spend(db, make_source(db), 8.5)
    recap = build_recap(db)
    assert "over 80% of the budget spent" in recap.subject and "most approved events per dollar first" in recap.text


def test_best_first_ranks_by_approved_events_per_dollar(db):
    pricey, cheap, nothing = other_source(db, "pricey"), other_source(db, "cheap"), other_source(db, "nothing")
    spend(db, pricey, 2); approve(db, pricey, 4)
    spend(db, cheap, 0.5); approve(db, cheap, 4)
    spend(db, nothing, 1)
    assert best_first(db, [nothing, pricey, cheap]) == [cheap, pricey, nothing]


def test_a_new_source_ranks_at_the_average_and_a_free_one_above_it(db):
    best, poor, new = other_source(db, "best"), other_source(db, "poor"), other_source(db, "new")
    free = other_source(db, "free")  # read from its own event data or API: no tokens at all
    spend(db, best, 0.5); approve(db, best, 10)
    spend(db, poor, 3); approve(db, poor, 1)
    approve(db, free, 2)
    assert best_first(db, [poor, new, best, free]) == [best, free, new, poor]


def test_one_cheap_check_doesnt_outrank_a_proven_source(db):
    proven, lucky, poor = other_source(db, "proven"), other_source(db, "lucky"), other_source(db, "poor")
    spend(db, proven, 1); approve(db, proven, 20)
    spend(db, lucky, 0.01); approve(db, lucky, 1)  # 100 events a dollar, but on one event
    spend(db, poor, 4)
    assert best_first(db, [lucky, proven, poor]) == [proven, lucky, poor]


def test_withdrawn_events_and_member_links_dont_count(db):
    a, b = other_source(db, "a"), other_source(db, "b")
    spend(db, a, 1); approve(db, a, 2)
    spend(db, b, 1); approve(db, b, 1); approve(db, b, 5, EventStatus.withdrawn)
    db.add(DiscoveryCheck(source_id=None, what="link", status="ok", output_tokens=10_000_000)); db.commit()
    assert best_first(db, [b, a]) == [a, b]


def test_check_now_goes_first_and_no_history_keeps_the_usual_order(db):
    a, b = other_source(db, "a"), other_source(db, "b")
    assert best_first(db, [b, a]) == [a, b]  # by id
    spend(db, a, 1); approve(db, a, 5)
    spend(db, b, 4)
    b.check_requested = True
    assert best_first(db, [a, b]) == [b, a]


def due_now(db, served, sources):
    for s in sources:
        s.next_check_on = None
        served[s.pages[0]] = {"html": "<p>season</p>"}
    db.commit()


def test_when_saving_the_best_source_is_read_before_the_money_runs_out(db, pages, monkeypatch):  # noqa: F811
    served, _ = pages
    poor, best = other_source(db, "poor"), other_source(db, "best")
    due_now(db, served, [poor, best])
    set_monthly_budget(db, 10)
    spend(db, poor, 8); approve(db, poor, 1)
    spend(db, best, 0.5); approve(db, best, 5)

    def costly(db_, source, client, today):  # this read uses up the budget
        spend(db_, source, 2)
        return runner.Outcome(status="ok")
    monkeypatch.setattr(runner, "check_source", costly)
    out = runner.run(db, FakeClaude([]), TODAY)
    assert [(name, o.status) for name, o in out[:2]] == [("best", "ok"), ("poor", "waiting")]


def test_with_budget_to_spare_the_order_doesnt_change(db, pages):  # noqa: F811
    served, _ = pages
    poor, best = other_source(db, "poor"), other_source(db, "best")
    due_now(db, served, [poor, best])
    spend(db, poor, 2); approve(db, poor, 1)
    spend(db, best, 0.5); approve(db, best, 5)
    assert [name for name, _ in runner.run(db, FakeClaude([]), TODAY)[:2]] == ["poor", "best"]


def test_structured_data_is_still_read_first_when_saving(db, pages):  # noqa: F811
    served, _ = pages
    shows = ",".join(ld_event(n, "2027-01-10", "2027-02-14", "35") for n in ("Hamlet", "Macbeth", "Cymbeline"))
    s = make_source(db)
    served["https://goodman.example/season"] = {"html": LD % shows}
    spend(db, s, 8.5)
    claude = FakeClaude([])
    out = runner.run(db, claude, TODAY)[0][1]
    assert claude.calls == 0 and out.new == 3
