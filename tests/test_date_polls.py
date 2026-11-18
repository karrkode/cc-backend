"""CC-297: crew date polls: who can see them, how answers and the best option work, and what leaving or deleting clears."""
from datetime import timedelta

from sqlalchemy import select

from app.models import DatePoll, DatePollAnswer, DatePollPick, Event
from app.stats import today_in_chicago

from .conftest import auth, register

T = today_in_chicago()


def d(n):
    return (T + timedelta(days=n)).isoformat()


def setup(client, make_event):
    """Ana leads Thursday Theater with Ben and Cy in it; Dee is in no crew. Hamlet runs for 30 days."""
    p = {n: auth(register(client, email=f"{n.lower()}@example.com", name=f"{n} Smith")) for n in ("Ana", "Ben", "Cy", "Dee")}
    crew = client.post("/crews", json={"name": "Thursday Theater"}, headers=p["Ana"]).json()
    for n in ("Ben", "Cy"):
        token = client.post(f"/crews/{crew['id']}/invites", headers=p["Ana"]).json()["token"]
        assert client.post("/crews/join", json={"token": token}, headers=p[n]).status_code in (200, 201)
    return p, crew, make_event("Hamlet")


def body(ev, options=((2, "19:30"), (3, "19:30"), (3, "14:00")), lo=1, hi=10):
    return {"event_id": ev.id, "window_from": d(lo), "window_to": d(hi), "options": [{"date": d(n), "time": t} for n, t in options]}


def start(client, h, crew, ev, **kw):
    r = client.post(f"/crews/{crew['id']}/polls", json=body(ev, **kw), headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def answer(client, h, crew, poll, picks):
    ids = [o["id"] for o in poll["options"] if (o["date"], o["time"]) in {(d(n), t) for n, t in picks}]
    return client.put(f"/crews/{crew['id']}/polls/{poll['id']}/answer", json={"option_ids": ids}, headers=h)


def test_start_validation(client, make_event):
    p, crew, ev = setup(client, make_event)
    url = f"/crews/{crew['id']}/polls"
    assert client.post(url, json=body(ev), headers=p["Dee"]).status_code == 404  # not a member
    assert client.post(url, json=body(ev)).status_code == 401
    art = make_event("Sue the T. rex", category="Visual Art")
    assert client.post(url, json=body(art), headers=p["Ben"]).status_code == 422  # an exhibition
    assert client.post(url, json=body(make_event("Hidden", status="pending")), headers=p["Ben"]).status_code == 404
    for bad in (dict(options=[(0, "19:30")]),  # today is in the window? lo=1 -> outside the window
                dict(options=[(11, "19:30")]),  # after the window
                dict(options=[(2, "7:30 pm")]),  # bad time
                dict(options=[(2, "19:30"), (2, "19:30")]),  # twice
                dict(options=[(2, "25:00")]),
                dict(lo=-3, hi=5, options=[(-1, "19:30")]),  # in the past
                dict(lo=1, hi=70, options=[(2, "19:30")]),  # window over 60 days
                dict(lo=5, hi=2, options=[(3, "19:30")])):  # backwards
        assert client.post(url, json=body(ev, **bad), headers=p["Ben"]).status_code == 422, bad
    too_many = [(1 + i // 3, f"{10 + i % 3 * 3:02d}:00") for i in range(41)]
    assert client.post(url, json=body(ev, options=too_many, hi=20), headers=p["Ben"]).status_code == 422
    out = start(client, p["Ben"], crew, ev)
    assert client.post(url, json=body(ev), headers=p["Cy"]).status_code == 409  # a second open one
    assert out["started_by"] == "Ben" and out["can_manage"] is True and out["members_total"] == 3 and out["status"] == "open"
    assert [(o["date"], o["time"]) for o in out["options"]] == [(d(2), "19:30"), (d(3), "14:00"), (d(3), "19:30")]
    other = make_event("Macbeth")
    assert client.post(url, json=body(other), headers=p["Cy"]).status_code == 201  # another event is fine


def test_event_must_still_be_running_for_each_option(client, make_event):
    p, crew, _ = setup(client, make_event)
    short = make_event("Short run", begin=T, end=T + timedelta(days=3))
    r = client.post(f"/crews/{crew['id']}/polls", json=body(short, options=[(5, "19:30")]), headers=p["Ana"])
    assert r.status_code == 422


def test_answer_replaces_and_empty_counts(client, make_event):
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ana"], crew, ev)
    r = answer(client, p["Ben"], crew, poll, [(2, "19:30"), (3, "19:30")]).json()
    assert r["answered_count"] == 1 and r["you_answered"] and sum(o["yours"] for o in r["options"]) == 2
    r = answer(client, p["Ben"], crew, poll, [(3, "14:00")]).json()  # replaces
    assert [o["yours"] for o in r["options"]] == [False, True, False] and r["answered_count"] == 1
    r = answer(client, p["Cy"], crew, poll, []).json()  # none of these work
    assert r["answered_count"] == 2 and r["you_answered"] and all(not o["yours"] for o in r["options"])
    assert client.put(f"/crews/{crew['id']}/polls/{poll['id']}/answer", json={"option_ids": [999999]}, headers=p["Ben"]).status_code == 422
    assert client.put(f"/crews/{crew['id']}/polls/{poll['id']}/answer", json={"option_ids": []}, headers=p["Dee"]).status_code == 404


def test_best_everyone_then_most_then_earliest(client, make_event):
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ana"], crew, ev, options=[(2, "19:30"), (3, "14:00"), (3, "19:30"), (4, "19:30")])
    answer(client, p["Ana"], crew, poll, [(3, "19:30"), (2, "19:30"), (4, "19:30")])
    answer(client, p["Ben"], crew, poll, [(3, "19:30"), (2, "19:30"), (3, "14:00")])
    r = answer(client, p["Cy"], crew, poll, [(3, "19:30"), (3, "14:00")]).json()
    best = r["best"]
    assert [(b["date"], b["time"], b["count"], b["everyone"]) for b in best] == [(d(3), "19:30", 3, True), (d(2), "19:30", 2, False), (d(3), "14:00", 2, False)]
    assert best[0]["names"] == ["Ana", "Ben", "Cy"]
    # a tie goes to the earliest; the 4th day (one tick) isn't in the top three
    assert all(b["date"] != d(4) for b in best)


def test_leaver_doesnt_count_and_answers_go(client, make_event, db):
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ana"], crew, ev)
    answer(client, p["Ben"], crew, poll, [(2, "19:30")])
    answer(client, p["Cy"], crew, poll, [(2, "19:30")])
    assert client.post(f"/crews/{crew['id']}/leave", headers=p["Cy"]).status_code == 204
    r = client.get(f"/crews/{crew['id']}/polls", headers=p["Ana"]).json()[0]
    assert r["members_total"] == 2 and r["answered_count"] == 1 and r["best"][0]["names"] == ["Ben"]
    assert db.scalar(select(DatePollAnswer).where(DatePollAnswer.poll_id == poll["id"], DatePollAnswer.user_id != None)) is not None  # noqa: E711
    assert len(db.scalars(select(DatePollAnswer)).all()) == 1 and len(db.scalars(select(DatePollPick)).all()) == 1
    # removal clears too
    client.post("/crews/join", json={"token": client.post(f"/crews/{crew['id']}/invites", headers=p["Ana"]).json()["token"]}, headers=p["Cy"])
    ben = next(m for m in client.get("/crews", headers=p["Ana"]).json()[0]["members"] if m["name"] == "Ben Smith")
    assert client.delete(f"/crews/{crew['id']}/members/{ben['user_id']}", headers=p["Ana"]).status_code == 200
    db.expire_all()
    assert db.scalars(select(DatePollAnswer)).all() == [] and db.scalars(select(DatePollPick)).all() == []
    # and a re-joiner starts clean, and can answer
    assert answer(client, p["Cy"], crew, poll, [(3, "19:30")]).status_code == 200


def test_lock_and_cancel(client, make_event):
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ben"], crew, ev)
    url = f"/crews/{crew['id']}/polls/{poll['id']}"
    opt = poll["options"][1]["id"]
    answer(client, p["Cy"], crew, poll, [(3, "14:00")])
    assert client.post(url + "/lock", json={"option_id": opt}, headers=p["Cy"]).status_code == 403
    assert client.post(url + "/cancel", headers=p["Ana"]).status_code == 403  # the leader isn't the starter while Ben is in
    assert client.post(url + "/lock", json={"option_id": 999999}, headers=p["Ben"]).status_code == 422
    assert client.post(url + "/lock", json={"option_id": opt}, headers=p["Dee"]).status_code == 404
    r = client.post(url + "/lock", json={"option_id": opt}, headers=p["Ben"]).json()
    assert r["status"] == "locked" and r["locked"]["date"] == d(3) and r["locked"]["yours"] is False and r["locked"]["names"] == ["Cy"]
    assert answer(client, p["Cy"], crew, poll, [(2, "19:30")]).status_code == 409  # refused after lock
    assert client.post(url + "/cancel", headers=p["Ben"]).status_code == 409
    again = start(client, p["Ana"], crew, ev)  # a locked poll doesn't block a new one
    assert client.post(f"/crews/{crew['id']}/polls/{again['id']}/cancel", headers=p["Ana"]).json()["status"] == "cancelled"
    assert answer(client, p["Cy"], crew, again, [(2, "19:30")]).status_code == 409


def test_leader_manages_once_starter_has_left(client, make_event):
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ben"], crew, ev)
    client.post(f"/crews/{crew['id']}/leave", headers=p["Ben"])
    r = client.get(f"/crews/{crew['id']}/polls", headers=p["Ana"]).json()[0]
    assert r["can_manage"] is True and r["started_by"] == "A former member"
    assert client.get(f"/crews/{crew['id']}/polls", headers=p["Cy"]).json()[0]["can_manage"] is False
    assert client.post(f"/crews/{crew['id']}/polls/{poll['id']}/cancel", headers=p["Cy"]).status_code == 403
    assert client.post(f"/crews/{crew['id']}/polls/{poll['id']}/cancel", headers=p["Ana"]).status_code == 200


def test_outsiders_and_other_crews_get_404(client, make_event):
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ana"], crew, ev)
    other = client.post("/crews", json={"name": "Other"}, headers=p["Dee"]).json()
    url = f"/crews/{other['id']}/polls/{poll['id']}"  # another crew's id with this poll's id
    assert client.put(url + "/answer", json={"option_ids": []}, headers=p["Dee"]).status_code == 404
    assert client.post(url + "/cancel", headers=p["Dee"]).status_code == 404
    assert client.post(url + "/lock", json={"option_id": poll["options"][0]["id"]}, headers=p["Dee"]).status_code == 404
    assert client.get(f"/crews/{crew['id']}/polls", headers=p["Dee"]).status_code == 404
    # a poll's option ids can't be used from another poll of the member's own crew
    mine = start(client, p["Dee"], other, ev)
    r = client.put(f"/crews/{other['id']}/polls/{mine['id']}/answer", json={"option_ids": [poll["options"][0]["id"]]}, headers=p["Dee"])
    assert r.status_code == 422
    assert client.get("/crews/polls", headers=p["Dee"]).json() == [] or all(x["crew_id"] == other["id"] for x in client.get("/crews/polls", headers=p["Dee"]).json())
    assert client.get(f"/crews/polls?event_id={ev.id}", headers=p["Dee"]).json()[0]["crew_id"] == other["id"]
    assert len(client.get(f"/crews/polls?event_id={ev.id}", headers=p["Dee"]).json()) == 1


def test_waiting_list(client, make_event):
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ana"], crew, ev)
    w = lambda who: client.get("/crews/polls?waiting=true", headers=p[who]).json()  # noqa: E731
    assert [x["id"] for x in w("Ben")] == [poll["id"]] and [x["id"] for x in w("Ana")] == [poll["id"]]
    assert w("Dee") == []
    answer(client, p["Ben"], crew, poll, [])
    assert w("Ben") == [] and len(w("Cy")) == 1
    client.post(f"/crews/{crew['id']}/polls/{poll['id']}/cancel", headers=p["Ana"])
    assert w("Cy") == [] and len(client.get("/crews/polls", headers=p["Cy"]).json()) == 1  # still listed, recently closed


def test_ended_when_last_option_has_passed(client, make_event, db):
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ana"], crew, ev, options=[(1, "19:30")])
    from app.models import DatePollOption
    opt = db.scalar(select(DatePollOption).where(DatePollOption.poll_id == poll["id"]))
    opt.date = T - timedelta(days=1)
    db.commit()
    r = client.get(f"/crews/{crew['id']}/polls", headers=p["Ben"]).json()[0]
    assert r["status"] == "ended"
    assert client.get("/crews/polls?waiting=true", headers=p["Ben"]).json() == []
    assert answer(client, p["Ben"], crew, poll, []).status_code == 409
    assert client.post(f"/crews/{crew['id']}/polls", json=body(ev), headers=p["Ben"]).status_code == 201  # one out of dates doesn't block a new one
    db.expire_all()
    assert db.get(DatePoll, poll["id"]).status == "ended"  # kept as ended, not turned into "cancelled"


def test_past_option_is_never_best_or_locked(client, make_event, db):
    from app.models import DatePollOption
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ana"], crew, ev, options=[(1, "19:30"), (3, "19:30")])
    answer(client, p["Ben"], crew, poll, [(1, "19:30"), (3, "19:30")])
    first = db.scalar(select(DatePollOption).where(DatePollOption.poll_id == poll["id"]).order_by(DatePollOption.date))
    first.date = T - timedelta(days=1)
    db.commit()
    r = client.get(f"/crews/{crew['id']}/polls", headers=p["Ana"]).json()[0]
    assert [b["date"] for b in r["best"]] == [d(3)]
    assert client.post(f"/crews/{crew['id']}/polls/{poll['id']}/lock", json={"option_id": first.id}, headers=p["Ana"]).status_code == 422


def test_deleting_an_account_deletes_its_polls_and_answers(client, make_event, db):
    from app.models import User
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ana"], crew, ev)
    answer(client, p["Ben"], crew, poll, [(2, "19:30")])
    ben = db.scalar(select(User).where(User.email == "ben@example.com"))
    db.delete(ben)
    db.commit()
    assert db.scalars(select(DatePollPick)).all() == [] and db.scalars(select(DatePollAnswer)).all() == []
    ana = db.scalar(select(User).where(User.email == "ana@example.com"))
    db.delete(ana)  # the starter, and the crew's owner: the crew and its poll go too
    db.commit()
    assert db.scalars(select(DatePoll)).all() == []


def test_cascades(client, make_event, db):
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ana"], crew, ev)
    answer(client, p["Ben"], crew, poll, [(2, "19:30")])
    assert client.delete(f"/crews/{crew['id']}", headers=p["Ana"]).status_code == 204
    db.expire_all()
    assert db.scalars(select(DatePoll)).all() == [] and db.scalars(select(DatePollPick)).all() == [] and db.scalars(select(DatePollAnswer)).all() == []
    p2, crew2, ev2 = setup2(client, make_event)
    start(client, p2["Ana"], crew2, ev2)
    db.delete(db.get(Event, ev2.id))
    db.commit()
    assert db.scalars(select(DatePoll)).all() == []


def setup2(client, make_event):
    p = {n: auth(register(client, email=f"{n.lower()}2@example.com", name=n)) for n in ("Ana",)}
    crew = client.post("/crews", json={"name": "Second"}, headers=p["Ana"]).json()
    return p, crew, make_event("Lear")


def test_no_email_anywhere_and_rate_limit(client, make_event):
    p, crew, ev = setup(client, make_event)
    poll = start(client, p["Ana"], crew, ev)
    answer(client, p["Ben"], crew, poll, [(2, "19:30")])
    for r in (client.get(f"/crews/{crew['id']}/polls", headers=p["Cy"]), client.get("/crews/polls", headers=p["Cy"]),
              answer(client, p["Cy"], crew, poll, [(2, "19:30")])):
        assert "@" not in r.text and "example.com" not in r.text
    from app.routers import polls
    polls.poll_limiter.reset()
    for i in range(20):
        e = make_event(f"Show {i}")
        assert client.post(f"/crews/{crew['id']}/polls", json=body(e), headers=p["Ben"]).status_code == 201
    assert client.post(f"/crews/{crew['id']}/polls", json=body(make_event("One more")), headers=p["Ben"]).status_code == 429
