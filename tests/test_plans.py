"""CC-54: your own plans for events: interested, or going on a date."""
from datetime import date, timedelta

from sqlalchemy import func, select

from app.models import EventStatus, Plan

from .conftest import auth, register

T = date.today()


def put(client, token, event_id, **body):
    params = body.pop("params", None)
    return client.put(f"/me/plans/{event_id}", headers=auth(token), json=body, params=params)


def plans(client, token):
    r = client.get("/me/plans", headers=auth(token))
    assert r.status_code == 200, r.text
    return [(p["event"]["title"], p["status"], p["date"]) for p in r.json()]


def test_interested_then_going_then_cleared(client, user_token, make_event):
    ev = make_event("Hamlet")
    r = put(client, user_token, ev.id, status="interested")
    assert r.status_code == 201 and r.json()["status"] == "interested" and r.json()["date"] is None
    r = put(client, user_token, ev.id, status="going", date=(T + timedelta(days=3)).isoformat())
    assert r.status_code == 200 and r.json()["status"] == "going"  # replaces the earlier plan
    assert plans(client, user_token) == [("Hamlet", "going", (T + timedelta(days=3)).isoformat())]
    assert client.delete(f"/me/plans/{ev.id}", headers=auth(user_token)).status_code == 204
    assert client.delete(f"/me/plans/{ev.id}", headers=auth(user_token)).status_code == 204  # idempotent
    assert plans(client, user_token) == []


def test_list_order_going_by_date_then_interested_newest(client, user_token, make_event):
    a, b, c, d = (make_event(t) for t in ("Later show", "Sooner show", "Liked first", "Liked second"))
    put(client, user_token, a.id, status="going", date=(T + timedelta(days=9)).isoformat())
    put(client, user_token, c.id, status="interested")
    put(client, user_token, b.id, status="going", date=(T + timedelta(days=2)).isoformat())
    put(client, user_token, d.id, status="interested")
    titles = [t for t, _, _ in plans(client, user_token)]
    assert titles[:2] == ["Sooner show", "Later show"]
    assert set(titles[2:]) == {"Liked first", "Liked second"}


def test_going_date_must_be_a_day_the_event_is_on(client, user_token, make_event):
    run = make_event("Short run", begin=T + timedelta(days=5), end=T + timedelta(days=8))
    for day, ok in ((5, True), (8, True), (4, False), (9, False)):
        r = put(client, user_token, run.id, status="going", date=(T + timedelta(days=day)).isoformat())
        assert (r.status_code == 200 or r.status_code == 201) == ok, (day, r.text)
    assert put(client, user_token, run.id, status="going", date=(T + timedelta(days=9)).isoformat()).json()["detail"] == \
        "The event isn't on that date"
    open_run = make_event("Open run", begin=None, end=None)
    assert put(client, user_token, open_run.id, status="going", date=(T + timedelta(days=200)).isoformat()).status_code == 201
    assert put(client, user_token, open_run.id, status="going", date=(T + timedelta(days=800)).isoformat()).status_code == 422


def test_no_past_dates_using_the_callers_day(client, user_token, make_event):
    ev = make_event("Hamlet", begin=T - timedelta(days=10), end=T + timedelta(days=10))
    r = put(client, user_token, ev.id, status="going", date=(T - timedelta(days=1)).isoformat())
    assert r.status_code == 422 and r.json()["detail"] == "That date has already passed"
    # A caller whose local day is still yesterday (a Chicago evening) may pick their today.
    yesterday = (T - timedelta(days=1)).isoformat()
    assert put(client, user_token, ev.id, status="going", date=yesterday, params={"today": yesterday}).status_code == 201
    # but a made-up "today" far in the past is ignored
    long_ago = (T - timedelta(days=5)).isoformat()
    assert put(client, user_token, ev.id, status="going", date=long_ago, params={"today": long_ago}).status_code == 422


def test_body_rules(client, user_token, make_event):
    ev = make_event("Hamlet")
    assert put(client, user_token, ev.id, status="going").status_code == 422  # going needs a date
    assert put(client, user_token, ev.id, status="interested", date=T.isoformat()).status_code == 422
    assert put(client, user_token, ev.id, status="maybe").status_code == 422


def test_only_live_events(client, user_token, make_event):
    pending = make_event("Pending", status=EventStatus.pending)
    assert put(client, user_token, pending.id, status="interested").status_code == 404
    assert put(client, user_token, 99999, status="interested").status_code == 404


def test_withdrawn_events_drop_out_and_deleted_ones_take_their_plans(client, user_token, admin_token, make_event, db):
    keep, hide, gone = make_event("Keep"), make_event("Hide"), make_event("Gone")
    for ev in (keep, hide, gone):
        put(client, user_token, ev.id, status="interested")
    client.patch(f"/events/{hide.id}", json={"status": "withdrawn"}, headers=auth(admin_token))
    client.delete(f"/events/{gone.id}", headers=auth(admin_token))
    assert [t for t, _, _ in plans(client, user_token)] == ["Keep"]
    assert db.scalar(select(func.count()).select_from(Plan)) == 2  # the withdrawn one is kept, in case it returns
    client.patch(f"/events/{hide.id}", json={"status": "approved"}, headers=auth(admin_token))
    assert {t for t, _, _ in plans(client, user_token)} == {"Keep", "Hide"}


def test_plans_are_private(client, user_token, admin_token, make_event):
    ev = make_event("Hamlet")
    put(client, user_token, ev.id, status="going", date=(T + timedelta(days=1)).isoformat())
    other = register(client, email="bo@example.com", name="Bo")
    assert plans(client, other) == []
    assert plans(client, admin_token) == []  # admins don't see other people's plans either
    assert client.delete(f"/me/plans/{ev.id}", headers=auth(other)).status_code == 204  # only clears Bo's own (none)
    assert len(plans(client, user_token)) == 1
    assert "plan" not in client.get(f"/events/{ev.id}", headers=auth(user_token)).text  # public shapes unchanged
    assert client.get("/me/plans").status_code == 401
