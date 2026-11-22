"""CC-298: crew date polls for exhibitions: up to five days, then half-hour availability inside the venue's hours."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import DatePoll, DatePollBlock, Event
from app.polls import best_ranges, hours_for, slots, valid_range
from app.stats import today_in_chicago

from .conftest import auth, register
from .test_date_polls import ready

T = today_in_chicago()


def d(n):
    return (T + timedelta(days=n)).isoformat()


def setup(client, make_event, db, hours=None):
    """Ana leads Art Crew with Ben and Cy; Dee is in no crew. The exhibition runs for 40 days; Ana and Ben share interest (a majority)."""
    p = {n: auth(register(client, email=f"{n.lower()}@example.com", name=f"{n} Smith")) for n in ("Ana", "Ben", "Cy", "Dee")}
    crew = client.post("/crews", json={"name": "Art Crew"}, headers=p["Ana"]).json()
    for n in ("Ben", "Cy"):
        token = client.post(f"/crews/{crew['id']}/invites", headers=p["Ana"]).json()["token"]
        assert client.post("/crews/join", json={"token": token}, headers=p[n]).status_code in (200, 201)
    ev = make_event("Sue the T. rex", category="Visual Art", venue_name="Field Museum", end=T + timedelta(days=40))
    if hours is not None:
        ev.venue.open_hours = hours
        db.commit()
    ready(client, crew, ev, p["Ana"], p["Ben"])
    return p, crew, ev


def start(client, h, crew, ev, days=(2, 3), expect=201):
    r = client.post(f"/crews/{crew['id']}/polls", json={"event_id": ev.id, "kind": "hours", "days": [d(n) for n in days]}, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


def mark(client, h, crew, poll, picks):
    """picks: {day offset: ["13:00", ...]}"""
    by = {o["date"]: o["id"] for o in poll["options"]}
    blocks = [{"option_id": by[d(n)], "start": t} for n, ts in picks.items() for t in ts]
    return client.put(f"/crews/{crew['id']}/polls/{poll['id']}/answer", json={"blocks": blocks}, headers=h)


def test_hours_helpers():
    wed = T + timedelta(days=(2 - T.weekday()) % 7)  # a Wednesday
    assert hours_for({}, wed) == ("10:00", "17:00")  # not known
    assert hours_for({"wed": ["09:00", "18:00"]}, wed) == ("09:00", "18:00")
    assert hours_for({"wed": None}, wed) is None  # closed
    assert hours_for({"wed": ["18:00", "09:00"]}, wed) == ("10:00", "17:00")  # nonsense counts as unknown
    assert hours_for({"wed": ["noon", "17:00"]}, wed) == ("10:00", "17:00")
    assert slots("10:00", "12:00") == ["10:00", "10:30", "11:00", "11:30"]
    assert slots("10:15", "11:30") == ["10:30", "11:00"]  # the grid starts on the half-hour
    assert slots(None, None) == []
    assert valid_range("10:00", "17:00", "13:00", "15:30") and valid_range("10:00", "17:00", "16:00", "17:00")
    assert not valid_range("10:00", "17:00", "13:00", "13:30")  # under an hour
    assert not valid_range("10:00", "17:00", "16:30", "17:30")  # past closing
    assert not valid_range("10:00", "17:00", "09:30", "11:00")  # before opening
    assert not valid_range("10:00", "17:00", "13:00", "14:15")  # off the grid


def test_start_validation(client, make_event, db):
    p, crew, ev = setup(client, make_event, db, hours={})
    url = f"/crews/{crew['id']}/polls"
    good = {"event_id": ev.id, "kind": "hours", "days": [d(2)]}
    assert client.post(url, json=good, headers=p["Dee"]).status_code == 404  # not a member
    assert client.post(url, json=good).status_code == 401
    for bad in ([], [d(1), d(2), d(3), d(4), d(5), d(6)],  # none, six
                [d(2), d(2)], [d(-1)], [d(30)], [d(45)]):  # twice, past, past the next 30 days, after the run
        assert client.post(url, json={**good, "days": bad}, headers=p["Ben"]).status_code == 422, bad
    assert client.post(url, json={**good, "options": [{"date": d(2), "time": ""}]}, headers=p["Ben"]).status_code == 422
    assert client.post(url, json={**good, "kind": "shows", "window_from": d(1), "window_to": d(5), "options": [{"date": d(2), "time": "10:00"}]}, headers=p["Ben"]).status_code == 422
    assert client.post(url, json={**good, "days": [d(29)]}, headers=p["Ben"]).status_code == 201  # the last day allowed
    crew2 = client.post("/crews", json={"name": "Other"}, headers=p["Dee"]).json()
    assert client.post(f"/crews/{crew2['id']}/polls", json=good, headers=p["Dee"]).status_code == 409  # no majority there


def test_five_days_and_a_second_poll(client, make_event, db):
    p, crew, ev = setup(client, make_event, db, hours={})
    out = start(client, p["Ben"], crew, ev, days=(9, 3, 5, 4, 7))
    assert out["kind"] == "hours" and [o["date"] for o in out["options"]] == [d(n) for n in (3, 4, 5, 7, 9)]
    assert out["window_from"] == d(3) and out["window_to"] == d(9)
    assert {(o["opens"], o["closes"], len(o["blocks"])) for o in out["options"]} == {("10:00", "17:00", 14)}
    start(client, p["Cy"], crew, ev, expect=409)


def test_exhibition_and_performance_kinds_dont_mix(client, make_event, db):
    p, crew, ev = setup(client, make_event, db)
    show = make_event("Hamlet")
    ready(client, crew, show, p["Ana"], p["Ben"])
    url = f"/crews/{crew['id']}/polls"
    assert client.post(url, json={"event_id": show.id, "kind": "hours", "days": [d(2)]}, headers=p["Ben"]).status_code == 422
    shows_body = {"event_id": ev.id, "window_from": d(1), "window_to": d(5), "options": [{"date": d(2), "time": "10:00"}]}
    assert client.post(url, json=shows_body, headers=p["Ben"]).status_code == 422  # what the live site would send
    ready_kinds = {r["event_id"]: r["kind"] for r in client.get("/crews/poll-ready", headers=p["Cy"]).json()}
    assert ready_kinds == {ev.id: "hours", show.id: "shows"}


def test_closed_day_and_the_hours_are_kept(client, make_event, db):
    wd = (T + timedelta(days=2)).weekday()
    key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][wd]
    p, crew, ev = setup(client, make_event, db, hours={key: None, ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][(wd + 1) % 7]: ["09:00", "11:00"]})
    start(client, p["Ben"], crew, ev, days=(2,), expect=422)  # closed that day
    out = start(client, p["Ben"], crew, ev, days=(3,))
    assert [(o["opens"], o["closes"], [b["start"] for b in o["blocks"]]) for o in out["options"]] == [("09:00", "11:00", ["09:00", "09:30", "10:00", "10:30"])]
    ev2 = db.get(Event, ev.id)
    ev2.venue.open_hours = {}  # the venue changes its hours later: the poll keeps the ones it started with
    db.commit()
    again = client.get(f"/crews/{crew['id']}/polls", headers=p["Cy"]).json()[0]
    assert again["options"][0]["closes"] == "11:00"


def test_answer_validation_and_replace(client, make_event, db):
    p, crew, ev = setup(client, make_event, db, hours={})
    poll = start(client, p["Ben"], crew, ev)
    url = f"/crews/{crew['id']}/polls/{poll['id']}/answer"
    a, b = (o["id"] for o in poll["options"])
    for bad in ([{"option_id": a, "start": "09:30"}],  # before opening
                [{"option_id": a, "start": "16:30"}, {"option_id": a, "start": "17:00"}],  # starts at closing
                [{"option_id": 999999, "start": "10:00"}],  # another poll's option
                [{"option_id": a, "start": "10:15"}]):  # off the half-hour
        assert client.put(url, json={"blocks": bad}, headers=p["Cy"]).status_code == 422, bad
    assert client.put(url, json={"option_ids": [a]}, headers=p["Cy"]).status_code == 422  # showtimes don't go to an hours poll
    assert mark(client, p["Cy"], crew, poll, {2: ["10:00", "10:30"], 3: ["10:00"]}).status_code == 200
    out = mark(client, p["Cy"], crew, poll, {2: ["13:00"]}).json()  # replaces
    day = {o["date"]: o for o in out["options"]}
    assert [x["start"] for x in day[d(2)]["blocks"] if x["yours"]] == ["13:00"] and not any(x["yours"] for x in day[d(3)]["blocks"])
    assert out["answered_count"] == 1 and out["you_answered"]
    none = mark(client, p["Ben"], crew, poll, {}).json()  # "none of these work" is an answer
    assert none["answered_count"] == 2 and none["best"] == []  # nobody has two neighbouring blocks in common yet (Cy has one)
    assert client.put(url, json={"blocks": [{"option_id": a, "start": "10:00"}]}, headers=p["Dee"]).status_code == 404


def _best(options, free, today=T, **kw):
    return best_ranges(options, free, {u: u for u in "abcde"}, 5, today=today, **kw)


class Opt:
    def __init__(self, id, day, opens="10:00", closes="17:00"):
        self.id, self.date, self.opens, self.closes = id, day, opens, closes


def test_best_ranges():
    o = Opt(1, T + timedelta(days=2))
    everyone = set("abcde")
    # all 5 free 1:00-3:30 PM: one range, everyone
    free = {1: {t: everyone for t in ("13:00", "13:30", "14:00", "14:30", "15:00")}}
    assert _best([o], free) == [{"option_id": 1, "date": o.date, "time": "13:00", "end": "15:30", "count": 5, "names": list("abcde"), "everyone": True}]
    # a single block is under an hour: no range
    assert _best([o], {1: {"13:00": everyone}}) == []
    # a gap splits the day; a longer one wins a tie on people
    free = {1: {"10:00": set("ab"), "10:30": set("ab"), "12:00": set("ab"), "12:30": set("ab"), "13:00": set("ab")}}
    got = _best([o], free)
    assert [(r["time"], r["end"]) for r in got] == [("12:00", "13:30"), ("10:00", "11:00")]
    # different people in neighbouring blocks: a range's people are those free throughout; more people ranks above longer
    free = {1: {"10:00": set("abc"), "10:30": set("abc"), "11:00": set("ab"), "11:30": set("ab")}}
    got = _best([o], free)
    assert [(r["time"], r["end"], r["count"], r["names"]) for r in got] == [("10:00", "11:00", 3, ["a", "b", "c"]), ("10:00", "12:00", 2, ["a", "b"])]
    # a day that has passed is never best
    assert _best([o], {1: {t: everyone for t in ("13:00", "13:30")}}, today=T + timedelta(days=3)) == []
    # only the top three
    day2, day3 = Opt(2, T + timedelta(days=3)), Opt(3, T + timedelta(days=4))
    free = {i: {"10:00": set("ab"), "10:30": set("ab")} for i in (1, 2, 3)}
    free[1] = {"10:00": set("abc"), "10:30": set("abc")}
    assert [(r["option_id"], r["count"]) for r in _best([o, day2, day3], free, limit=2)] == [(1, 3), (2, 2)]


def test_best_through_the_api_leaver_and_privacy(client, make_event, db):
    p, crew, ev = setup(client, make_event, db, hours={})
    poll = start(client, p["Ben"], crew, ev)
    for n in ("Ana", "Ben", "Cy"):
        out = mark(client, p[n], crew, poll, {2: ["13:00", "13:30", "14:00", "14:30"]}).json()
    assert out["best"][0]["time"] == "13:00" and out["best"][0]["end"] == "15:00" and out["best"][0]["everyone"] and out["best"][0]["names"] == ["Ana", "Ben", "Cy"]
    assert "@" not in client.get(f"/crews/{crew['id']}/polls", headers=p["Cy"]).text
    assert client.post(f"/crews/{crew['id']}/leave", headers=p["Cy"]).status_code in (200, 204)
    assert {b.user_id for b in db.scalars(select(DatePollBlock))} and len(db.scalars(select(DatePollBlock)).all()) == 8  # Cy's four went; Ana's and Ben's stay
    after = client.get(f"/crews/{crew['id']}/polls", headers=p["Ana"]).json()[0]
    assert after["best"][0]["names"] == ["Ana", "Ben"] and after["members_total"] == 2 and after["answered_count"] == 2


def test_lock_ranges_and_commit_data(client, make_event, db):
    p, crew, ev = setup(client, make_event, db, hours={})
    poll = start(client, p["Ben"], crew, ev)
    a = poll["options"][0]["id"]
    mark(client, p["Ben"], crew, poll, {2: ["13:00", "13:30", "14:00"]})
    mark(client, p["Cy"], crew, poll, {2: ["13:30", "14:00", "14:30"]})
    url = f"/crews/{crew['id']}/polls/{poll['id']}/lock"
    rng = {"option_id": a, "start": "13:00", "end": "15:00"}
    assert client.post(url, json=rng, headers=p["Ben"]).status_code == 403  # the leader only
    for bad in ({"option_id": a}, {"option_id": a, "start": "13:00", "end": "13:30"}, {"option_id": a, "start": "16:30", "end": "17:30"},
                {"option_id": a, "start": "13:00", "end": "14:45"}, {"option_id": 999999, "start": "13:00", "end": "14:00"}):
        assert client.post(url, json=bad, headers=p["Ana"]).status_code == 422, bad
    out = client.post(url, json=rng, headers=p["Ana"]).json()  # any range of the day, not only a best one
    assert out["status"] == "locked" and out["locked"]["time"] == "13:00" and out["locked"]["end"] == "15:00"
    assert out["locked"]["names"] == [] and out["locked"]["yours"] is False  # nobody is free for the whole of it
    assert client.put(f"/crews/{crew['id']}/polls/{poll['id']}/answer", json={"blocks": []}, headers=p["Cy"]).status_code == 409
    assert client.post(url, json=rng, headers=p["Ana"]).status_code == 409


def test_lock_names_who_can_make_it(client, make_event, db):
    p, crew, ev = setup(client, make_event, db, hours={})
    poll = start(client, p["Ben"], crew, ev)
    a = poll["options"][0]["id"]
    mark(client, p["Ben"], crew, poll, {2: ["13:00", "13:30", "14:00"]})
    mark(client, p["Cy"], crew, poll, {2: ["13:30", "14:00", "14:30"]})
    out = client.post(f"/crews/{crew['id']}/polls/{poll['id']}/lock", json={"option_id": a, "start": "13:30", "end": "14:30"}, headers=p["Ana"]).json()
    assert out["locked"]["names"] == ["Ben", "Cy"] and out["locked"]["yours"] is False
    mine = client.get(f"/crews/{crew['id']}/polls", headers=p["Cy"]).json()[0]
    assert mine["locked"]["yours"] is True  # Cy can Commit to it


def test_showtime_poll_refuses_hours_data(client, make_event, db):
    from .test_date_polls import setup as shows_setup, start as shows_start
    p, crew, ev = shows_setup(client, make_event)
    poll = shows_start(client, p["Ben"], crew, ev)
    o = poll["options"][0]
    assert client.put(f"/crews/{crew['id']}/polls/{poll['id']}/answer", json={"blocks": [{"option_id": o["id"], "start": "10:00"}]}, headers=p["Ben"]).status_code == 422
    assert client.post(f"/crews/{crew['id']}/polls/{poll['id']}/lock", json={"option_id": o["id"], "start": "10:00", "end": "11:00"}, headers=p["Ana"]).status_code == 422
    assert poll["kind"] == "shows" and o["blocks"] == [] and o["opens"] is None


def test_outsiders_cascades_and_ending(client, make_event, db):
    p, crew, ev = setup(client, make_event, db, hours={})
    poll = start(client, p["Ben"], crew, ev)
    mark(client, p["Cy"], crew, poll, {2: ["10:00"]})
    other = auth(register(client, email="eve@example.com", name="Eve Jones"))
    crew2 = client.post("/crews", json={"name": "Elsewhere"}, headers=other).json()
    for url, verb in ((f"/crews/{crew['id']}/polls", "get"), (f"/crews/{crew2['id']}/polls/{poll['id']}/answer", "put")):
        r = getattr(client, verb)(url, json={"blocks": []}, headers=other) if verb == "put" else client.get(url, headers=other)
        assert r.status_code == 404
    assert client.get("/crews/polls", headers=other).json() == []
    # the last day passes: it reads as ended
    row = db.get(DatePoll, poll["id"])
    for i, o in enumerate(row.options):
        o.date = T - timedelta(days=1 + i)
    db.commit()
    assert [x["status"] for x in client.get("/crews/polls", headers=p["Ana"]).json()] == ["ended"]
    assert client.put(f"/crews/{crew['id']}/polls/{poll['id']}/answer", json={"blocks": []}, headers=p["Cy"]).status_code == 409
    # deleting the event removes the poll and its blocks
    db.delete(db.get(Event, ev.id))
    db.commit()
    assert db.scalars(select(DatePollBlock)).all() == [] and db.scalars(select(DatePoll)).all() == []


def test_ready_only_when_a_day_can_be_picked(client, make_event, db):
    """An exhibition that opens in 45 days (or a venue closed every day) isn't 'ready': there'd be nothing to pick, and nothing to wait on."""
    p, crew, ev = setup(client, make_event, db, hours={})
    assert [r["kind"] for r in client.get("/crews/poll-ready", headers=p["Cy"]).json()] == ["hours"]
    ev = db.get(Event, ev.id)
    ev.begin_date, ev.end_date = T + timedelta(days=45), T + timedelta(days=80)
    db.commit()
    assert client.get("/crews/poll-ready", headers=p["Cy"]).json() == []
    ev.begin_date, ev.end_date = T - timedelta(days=5), T + timedelta(days=40)
    ev.venue.open_hours = {k: None for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    db.commit()
    assert client.get("/crews/poll-ready", headers=p["Cy"]).json() == []
    start(client, p["Ben"], crew, ev, days=(2,), expect=422)


def test_days_outside_the_run(client, make_event, db):
    p, crew, ev = setup(client, make_event, db, hours={})
    ev = db.get(Event, ev.id)
    ev.begin_date, ev.end_date = T + timedelta(days=5), T + timedelta(days=10)
    db.commit()
    start(client, p["Ben"], crew, ev, days=(2,), expect=422)  # before it opens
    start(client, p["Ben"], crew, ev, days=(15,), expect=422)  # after it closes
    start(client, p["Ben"], crew, ev, days=(5, 10))


def test_short_hours_count_as_unknown_and_odd_data_is_safe():
    wed = T + timedelta(days=(2 - T.weekday()) % 7)
    assert hours_for({"wed": ["10:00", "10:20"]}, wed) == ("10:00", "17:00")  # no block fits
    assert hours_for({"wed": ["10:15", "10:45"]}, wed) == ("10:00", "17:00")  # one block: no range
    assert hours_for(["not", "a dict"], wed) == ("10:00", "17:00")
    assert hours_for({"wed": ["10:00", 5]}, wed) == ("10:00", "17:00")
    assert not valid_range("10:00", "17:00", "13:00", "1٤:00")  # a non-ASCII digit is not a time


def test_lock_and_answer_use_this_polls_options_only(client, make_event, db):
    p, crew, ev = setup(client, make_event, db, hours={})
    mine = start(client, p["Ben"], crew, ev)
    other = auth(register(client, email="eve@example.com", name="Eve Jones"))
    crew2 = client.post("/crews", json={"name": "Elsewhere"}, headers=other).json()
    for n in ("Fay", "Gus"):
        h = auth(register(client, email=f"{n.lower()}@example.com", name=f"{n} Jones"))
        token = client.post(f"/crews/{crew2['id']}/invites", headers=other).json()["token"]
        assert client.post("/crews/join", json={"token": token}, headers=h).status_code in (200, 201)
    ready(client, crew2, ev, other, h)
    theirs = start(client, other, crew2, ev)
    foreign = theirs["options"][0]["id"]
    assert client.put(f"/crews/{crew['id']}/polls/{mine['id']}/answer", json={"blocks": [{"option_id": foreign, "start": "10:00"}]}, headers=p["Cy"]).status_code == 422
    assert client.post(f"/crews/{crew['id']}/polls/{mine['id']}/lock", json={"option_id": foreign, "start": "10:00", "end": "11:00"}, headers=p["Ana"]).status_code == 422
    assert client.put(f"/crews/{crew2['id']}/polls/{mine['id']}/answer", json={"blocks": []}, headers=other).status_code == 404  # another crew's poll id
    assert client.put(f"/crews/{crew['id']}/polls/{mine['id']}/answer", json={"blocks": [{"option_id": foreign, "start": "10:00"}] * 241}, headers=p["Cy"]).status_code == 422  # over the cap
