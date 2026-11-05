"""CC-207: the Event Sourcing page: hiding a found event, the graph of new events found, and the budget's figures."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.discovery.budget import set_monthly_budget
from app.discovery.needs import needs
from app.discovery.readers import FoundEvent, cost_usd
from app.discovery.recap import build_recap, found_by_week, gather
from app.discovery.run import check_details, check_missing, run
from app.discovery.store import PASSED_REASON, drop_passed_hidden, record
from app.models import DiscoveredItem, DiscoveryCheck, DiscoverySource, Event, SourceKind
from app.stats import today_in_chicago

from .conftest import auth
from .test_discovery import make_source


def found(title, url="", **kw):
    return FoundEvent(title=title, url=url, kind=kw.pop("kind", "production"), category=kw.pop("category", "Theater"), **kw)


def item_named(db, title):
    return db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == title))


def summary(client, token):
    return client.get("/admin/discovery/summary", headers=auth(token)).json()


def act(client, token, item, what, body=None):
    return client.post(f"/admin/discovery/items/{item.id}/{what}", json=body or {}, headers=auth(token))


def hidden_one(client, token, db, title="Hamlet", **kw):
    s = make_source(db) if "source" not in kw else kw.pop("source")
    record(db, s, found(title, f"https://goodman.example/{title.lower()}", **kw), "")
    db.commit()
    item = item_named(db, title)
    assert act(client, token, item, "hide").json()["status"] == "hidden"
    db.refresh(item)
    return s, item


# ---- hiding ---------------------------------------------------------------------------

def test_hiding_leaves_the_waiting_numbers_but_still_counts_as_found(client, db, admin_token):
    s, item = hidden_one(client, admin_token, db)
    record(db, s, found("Macbeth", begin_date="2027-01-10"), "")
    db.commit()
    b = summary(client, admin_token)
    assert b["new_waiting"] == 1 and b["hidden"] == 1
    assert [r["n"] for r in b["waiting_by_source"]] == [1]
    assert client.get("/admin/discovery/items?limit=1", headers=auth(admin_token)).json()["total"] == 1  # the Review count
    assert client.get("/admin/discovery/sources", headers=auth(admin_token)).json()["sources"][0]["waiting"] == 1
    assert b["found_total"] == 2 and b["found_weeks"][-1]["n"] == 2
    t = build_recap(db).text
    assert "Waiting for review: 1 (1 new, 0 proposed updates)" in t and "NEW EVENTS FOUND THIS WEEK: 2" in t


def test_a_hidden_event_is_listed_with_show_hidden(client, db, admin_token):
    _, item = hidden_one(client, admin_token, db)
    r = client.get("/admin/discovery/items?show=hidden", headers=auth(admin_token)).json()
    assert [i["id"] for i in r["items"]] == [item.id] and r["items"][0]["needs"] == "date"  # the approve form asks for it
    assert client.get("/admin/discovery/items", headers=auth(admin_token)).json()["total"] == 0


def test_a_later_check_leaves_it_hidden_and_as_it_was(client, db, admin_token):
    s, item = hidden_one(client, admin_token, db, cost_text="$20")
    assert record(db, s, found("Hamlet", item.url, cost_text="$99"), "") == "seen"
    db.commit()
    db.refresh(item)
    assert item.status == "hidden" and item.cost_text == "$20"


def test_its_own_page_isnt_read_while_hidden(client, db, admin_token):
    tomorrow = today_in_chicago() + timedelta(days=1)
    s, item = hidden_one(client, admin_token, db, begin_date=(tomorrow + timedelta(days=50)).isoformat())
    item.first_seen_at = datetime.now(UTC) - timedelta(days=2)
    db.commit()
    assert needs(item, None)  # it would be read for what's missing, and 60 days out, if it weren't hidden
    assert check_missing(db, object(), tomorrow) == [] and check_details(db, object(), tomorrow) == []
    assert item.looked_on == [] and item.detail_checks == []


def test_unhide_makes_it_new_again(client, db, admin_token):
    _, item = hidden_one(client, admin_token, db)
    assert act(client, admin_token, item, "unhide").json()["status"] == "new"
    b = summary(client, admin_token)
    assert b["new_waiting"] == 1 and b["hidden"] == 0


def test_only_new_events_hide_and_only_hidden_ones_unhide(client, db, admin_token):
    s = make_source(db)
    record(db, s, found("Hamlet"), "")
    record(db, s, found("Acting 101", kind="class"), "a class or workshop")
    db.commit()
    assert act(client, admin_token, item_named(db, "Acting 101"), "hide").status_code == 409
    assert act(client, admin_token, item_named(db, "Hamlet"), "unhide").status_code == 409


@pytest.mark.parametrize("kw, gone", [
    ({"end_date": -1}, True),
    ({"end_date": 0}, False),  # its last day is today
    ({"begin_date": -30, "end_date": -2}, True),
    ({}, False),  # no dates: the admin decides
    ({"begin_date": -30, "open_run": True}, False),
    ({"begin_date": -30}, False),  # no closing date: still on, as in the catalog
    ({"end_date": -1, "performances": [1]}, False),  # a later performance
    ({"performances": [-1]}, True),
])
def test_once_its_dates_have_passed_it_shows_as_rejected(client, db, admin_token, kw, gone):
    today = today_in_chicago()
    day = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    s, item = hidden_one(client, admin_token, db)
    item.begin_date = today + timedelta(days=kw["begin_date"]) if "begin_date" in kw else None
    item.end_date = today + timedelta(days=kw["end_date"]) if "end_date" in kw else None
    item.open_run = kw.get("open_run", False)
    item.performances = [{"date": day(n), "time": "19:30"} for n in kw.get("performances", [])]
    db.commit()
    assert drop_passed_hidden(db, today) == (1 if gone else 0)
    db.refresh(item)
    assert (item.status, item.skip_reason) == (("rejected", PASSED_REASON) if gone else ("hidden", ""))
    if gone:  # it can't come back, and it still counts as found
        assert record(db, s, found("Hamlet", item.url), "") == "seen"
        assert item.status == "rejected" and gather(db, datetime.now(UTC)).found_total == 1


def test_the_run_drops_passed_events_even_with_the_budget_used_up(client, db, admin_token):
    today = today_in_chicago()
    _, item = hidden_one(client, admin_token, db)
    item.end_date = today - timedelta(days=1)
    set_monthly_budget(db, 1)
    db.add(DiscoveryCheck(source_id=item.source_id, status="ok", pages_read=1, input_tokens=100_000_000, output_tokens=0))
    db.commit()
    run(db, None, today)
    db.refresh(item)
    assert item.status == "rejected"


# ---- acting on one --------------------------------------------------------------------

def test_delete_works_on_a_hidden_event_and_a_later_check_doesnt_bring_it_back(client, db, admin_token):
    s, item = hidden_one(client, admin_token, db)
    assert act(client, admin_token, item, "reject").status_code == 200
    assert record(db, s, found("Hamlet", item.url), "") == "seen"
    db.commit()
    db.refresh(item)
    assert item.status == "rejected" and summary(client, admin_token)["hidden"] == 0


def test_a_new_event_deleted_doesnt_come_back(client, db, admin_token):
    s = make_source(db)
    record(db, s, found("Hamlet", "https://goodman.example/hamlet"), "")
    db.commit()
    item = item_named(db, "Hamlet")
    assert act(client, admin_token, item, "reject").status_code == 200
    assert record(db, s, found("Hamlet", "https://goodman.example/hamlet"), "") == "seen"
    db.commit()
    db.refresh(item)
    assert item.status == "rejected"


def test_a_hidden_event_can_be_approved(client, db, admin_token):
    s, item = hidden_one(client, admin_token, db, begin_date="2027-01-10", end_date="2027-02-10")
    body = {"title": "Hamlet", "category": "Theater", "venue_id": s.venue_id, "begin_date": "2027-01-10", "end_date": "2027-02-10",
            "weekly": {"thu": ["19:30"]}}
    r = act(client, admin_token, item, "approve", body)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert db.scalar(select(Event).where(Event.title == "Hamlet")) is not None
    assert summary(client, admin_token)["hidden"] == 0


# ---- the found events list ------------------------------------------------------------

def test_sources_are_largest_first_with_their_ids_and_a_deleted_source_can_be_opened(client, db, admin_token):
    small = make_source(db)
    big = DiscoverySource(name="Steppenwolf", kind=SourceKind.theater, pages=["https://steppenwolf.example/"], reader="claude", every_days=30,
                          venue_name="Steppenwolf", neighborhood="Lincoln Park", page_state={}, venue_hours={}, unchanged_streak=0,
                          last_status="", last_error="")
    db.add(big)
    db.commit()
    record(db, small, found("Hamlet"), "")
    record(db, big, found("Macbeth"), "")
    record(db, big, found("Lear"), "")
    db.commit()
    h = auth(admin_token)
    assert [(r["id"], r["n"]) for r in summary(client, admin_token)["waiting_by_source"]] == [(big.id, 2), (small.id, 1)]
    assert client.delete(f"/admin/discovery/sources/{big.id}", headers=h).status_code == 204
    rows = summary(client, admin_token)["waiting_by_source"]
    assert rows[0] == {"id": None, "name": "Deleted source", "n": 2}
    listed = client.get("/admin/discovery/items?show=new&source=0", headers=h).json()
    assert {i["title"] for i in listed["items"]} == {"Macbeth", "Lear"}


def test_a_long_list_loads_in_parts(client, db, admin_token):
    s = make_source(db)
    for n in range(5):
        record(db, s, found(f"Show {n}"), "")
    db.commit()
    url = f"/admin/discovery/items?show=new&source={s.id}&limit=2"
    first = client.get(url, headers=auth(admin_token)).json()
    rest = client.get(url + "&offset=2", headers=auth(admin_token)).json()
    assert first["total"] == 5 and len(first["items"]) == len(rest["items"]) == 2
    assert not {i["id"] for i in first["items"]} & {i["id"] for i in rest["items"]}


# ---- the graph ------------------------------------------------------------------------

def seen(db, source, title, days_ago, now, status="new", reason=""):
    db.add(DiscoveredItem(source_id=source.id, key=title, title=title, status=status, skip_reason=reason,
                          first_seen_at=now - timedelta(days=days_ago), last_seen_at=now, changes={}, detail_checks=[]))


def test_the_graph_counts_like_the_recap(db):
    now = datetime(2026, 11, 5, 18, tzinfo=UTC)
    s = make_source(db)
    seen(db, s, "This week", 1, now)
    seen(db, s, "Hidden this week", 2, now, "hidden")
    seen(db, s, "Exactly a week ago", 7, now, "approved")
    seen(db, s, "Rejected", 3, now, "rejected")
    seen(db, s, "Passed", 3, now, "rejected", PASSED_REASON)
    seen(db, s, "Caught by a rule", 3, now, "rejected", "skip rule: “student”")
    seen(db, s, "Left out", 3, now, "skipped", "a class or workshop")
    seen(db, s, "Listed", 3, now, "listed")
    seen(db, s, "Last week", 8, now)
    seen(db, s, "Three weeks back", 20, now)
    db.commit()
    weeks = found_by_week(db, now)
    assert weeks[-1][2] == gather(db, now).found_total == 5
    assert [n for _, _, n in weeks] == [1, 1, 5]  # it began in the third week back: earlier weeks are left out
    w = gather(db, now)
    assert (weeks[-1][0], weeks[-1][1]) == (w.first, w.last)


def test_the_graph_has_twelve_weeks_at_most_and_nothing_before_discovery_began(db):
    now = datetime(2026, 11, 5, 18, tzinfo=UTC)
    assert found_by_week(db, now) == []
    s = make_source(db)
    seen(db, s, "Long ago", 200, now)
    seen(db, s, "Eighty-four days", 84, now)
    db.commit()
    weeks = found_by_week(db, now)
    assert len(weeks) == 12 and weeks[0][2] == 1 and sum(n for _, _, n in weeks) == 1


def test_a_check_that_found_nothing_starts_the_graph(db):
    now = datetime.now(UTC)
    s = make_source(db)
    db.add(DiscoveryCheck(source_id=s.id, status="ok", pages_read=0, input_tokens=0, output_tokens=0, checked_at=now - timedelta(days=9)))
    db.commit()
    assert [n for _, _, n in found_by_week(db, now)] == [0, 0]


# ---- the budget -----------------------------------------------------------------------

@pytest.mark.parametrize("share, state", [(0.79, "ok"), (0.80, "saving"), (0.99, "saving"), (1.0, "paused"), (1.5, "paused")])
def test_the_page_gets_the_budget_guards_own_figures(client, db, admin_token, share, state):
    s = make_source(db)
    tokens = 10_000_000
    set_monthly_budget(db, round(cost_usd(tokens, 0) / share, 6))
    db.add(DiscoveryCheck(source_id=s.id, status="ok", pages_read=1, input_tokens=tokens, output_tokens=0))
    db.commit()
    b = summary(client, admin_token)
    assert b["budget_state"] == state and b["spent_month_usd"] == cost_usd(tokens, 0)
    assert (b["budget_note"] != "") == (state != "ok")


# ---- who ------------------------------------------------------------------------------

@pytest.mark.parametrize("what", ["hide", "unhide"])
def test_only_admins_hide_and_unhide(client, db, user_token, what):
    s = make_source(db)
    record(db, s, found("Hamlet"), "")
    db.commit()
    item = item_named(db, "Hamlet")
    assert client.post(f"/admin/discovery/items/{item.id}/{what}", json={}).status_code == 401
    assert client.post(f"/admin/discovery/items/{item.id}/{what}", json={}, headers=auth(user_token)).status_code == 403
    assert client.get("/admin/discovery/items?show=hidden", headers=auth(user_token)).status_code == 403
