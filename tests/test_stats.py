"""CC-67: the admin Stats page: weekly sign-ups and member activity, totals only."""
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select

from app.models import ActiveDay, Attended, Plan, PlanStatus, Role, User
from app.stats import CHICAGO, today_in_chicago, weekly_stats

from .conftest import auth, register

TODAY = today_in_chicago()
MONDAY = TODAY - timedelta(days=TODAY.weekday())


def at(day, hour=12):
    """Noon Chicago time on that day, as the UTC timestamp the database keeps."""
    return datetime.combine(day, time(hour), tzinfo=CHICAGO).astimezone(UTC)


def member(db, email, joined, verified=True, role=Role.user):
    u = User(email=email, password_hash="x", role=role, created_at=at(joined),
             email_verified_at=at(joined) if verified else None)
    db.add(u); db.flush()
    return u


def test_signing_in_records_one_active_day(client, db):
    register(client, email="ana@example.com", password="correct-horse-1")
    for _ in range(3):
        client.post("/auth/login", json={"email": "ana@example.com", "password": "correct-horse-1"})
    assert db.scalar(select(func.count()).select_from(ActiveDay)) == 1
    assert db.scalar(select(ActiveDay.day)) == TODAY


def test_weekly_numbers(db, make_event):
    ev, ev2 = make_event("Hamlet"), make_event("Bug")
    a = member(db, "a@example.com", MONDAY)  # this week
    b = member(db, "b@example.com", MONDAY - timedelta(days=1), verified=False)  # last week (Sunday)
    member(db, "boss@example.com", MONDAY, role=Role.admin)  # admins aren't counted
    db.add_all([ActiveDay(user_id=a.id, day=d) for d in {MONDAY, TODAY}])  # one member, maybe on two days
    db.add_all([
        ActiveDay(user_id=b.id, day=MONDAY - timedelta(days=3)),
        Plan(user_id=a.id, event_id=ev.id, status=PlanStatus.interested, created_at=at(MONDAY)),
        Plan(user_id=b.id, event_id=ev.id, status=PlanStatus.going, going_on=TODAY, created_at=at(MONDAY - timedelta(days=2))),
        Attended(user_id=a.id, event_id=ev2.id, created_at=at(TODAY)),
    ])
    db.commit()
    s = weekly_stats(db, TODAY, weeks=3)
    assert [w["start"] for w in s["weeks"]] == [MONDAY - timedelta(weeks=2), MONDAY - timedelta(weeks=1), MONDAY]
    this, last = s["weeks"][-1], s["weeks"][-2]
    assert (this["registrations"], this["confirmed"], this["active"], this["plans_interested"], this["attended"]) == (1, 1, 1, 1, 1)
    assert (last["registrations"], last["confirmed"], last["active"], last["plans_going"]) == (1, 0, 1, 1)
    assert (s["members"], s["confirmed_members"]) == (2, 1)


def test_retention_counts_members_back_after_four_weeks(db):
    start = TODAY - timedelta(days=60)
    early = member(db, "early@example.com", start)      # came back on day 30: retained
    gone = member(db, "gone@example.com", start)        # only used it the first week
    member(db, "new@example.com", TODAY - timedelta(days=5))  # too new to count
    db.add_all([ActiveDay(user_id=early.id, day=start), ActiveDay(user_id=early.id, day=start + timedelta(days=30)),
                ActiveDay(user_id=gone.id, day=start + timedelta(days=2))])
    db.commit()
    s = weekly_stats(db, TODAY)
    assert s["retention"] == {"eligible": 2, "retained": 1}
    assert s["active_since"] == start


def test_admins_only_and_totals_only(client, user_token, admin_token):
    assert client.get("/admin/stats", headers=auth(user_token)).status_code == 403
    assert client.get("/admin/stats").status_code == 401
    r = client.get("/admin/stats", params={"weeks": 4}, headers=auth(admin_token))
    assert r.status_code == 200 and len(r.json()["weeks"]) == 4
    assert "@" not in r.text  # no emails, no names: totals only
    far = (TODAY - timedelta(days=30)).isoformat()  # an implausible "today" is ignored
    assert client.get("/admin/stats", params={"today": far}, headers=auth(admin_token)).json()["today"] == TODAY.isoformat()


def test_event_interest_ranking(client, db, admin_token, make_event):
    """CC-69: events ranked by members' interest + commitments; admins left out; totals only."""
    from app.models import EventStatus
    hamlet, bug, quiet, closed = make_event("Hamlet"), make_event("Bug"), make_event("Quiet"), \
        make_event("Closed", begin=TODAY - timedelta(days=20), end=TODAY - timedelta(days=2))
    hidden = make_event("Hidden", status=EventStatus.withdrawn)
    a, b, c = (member(db, f"{n}@example.com", TODAY - timedelta(days=40)) for n in "abc")
    boss = member(db, "boss@example.com", TODAY - timedelta(days=40), role=Role.admin)
    old = at(TODAY - timedelta(days=45))
    db.add_all([
        Plan(user_id=a.id, event_id=hamlet.id, status=PlanStatus.interested, created_at=at(TODAY)),
        Plan(user_id=b.id, event_id=hamlet.id, status=PlanStatus.going, going_on=TODAY, created_at=at(TODAY)),
        Plan(user_id=c.id, event_id=bug.id, status=PlanStatus.going, going_on=TODAY, created_at=at(TODAY)),
        Plan(user_id=a.id, event_id=bug.id, status=PlanStatus.interested, created_at=old),  # outside 30 days
        Plan(user_id=boss.id, event_id=quiet.id, status=PlanStatus.going, going_on=TODAY, created_at=at(TODAY)),  # admin
        Plan(user_id=a.id, event_id=closed.id, status=PlanStatus.interested, created_at=at(TODAY)),
        Plan(user_id=b.id, event_id=hidden.id, status=PlanStatus.interested, created_at=at(TODAY)),
        Attended(user_id=c.id, event_id=hamlet.id, created_at=at(TODAY)),
    ])
    db.commit()
    h = auth(admin_token)

    def ranked(**params):
        r = client.get("/admin/stats/events", params=params, headers=h)
        assert r.status_code == 200, r.text
        return [(x["event"]["title"], x["interested"], x["going"], x["attended"]) for x in r.json()]

    assert ranked() == [("Hamlet", 1, 1, 1), ("Bug", 0, 1, 0)]  # last 30 days, current events, members only
    assert ranked(range="all") == [("Hamlet", 1, 1, 1), ("Bug", 1, 1, 0)]  # a tie: more committed, then more "I've been"
    assert ("Closed", 1, 0, 0) in ranked(include_closed="true")
    assert all(t != "Hidden" for t, *_ in ranked(range="all", include_closed="true"))  # withdrawn events never show
    assert ranked(limit=1) == [("Hamlet", 1, 1, 1)]
    r = client.get("/admin/stats/events", headers=h)
    assert "@example.com" not in r.text  # never who
    assert client.get("/admin/stats/events", headers=auth(register(client, email="z@example.com"))).status_code == 403
