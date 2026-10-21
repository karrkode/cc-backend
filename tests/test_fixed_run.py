"""CC-96: a fixed run: an event on chosen dates only, with a time for each (up to 30), set by the submitter
and editable by admins. The catalog's date filters match only those dates, not the days in between."""
from datetime import timedelta

from app.stats import today_in_chicago

from .conftest import auth

T = today_in_chicago()
VENUE = {"name": "Tiny Stage", "neighborhood": "Oak Park, IL"}


def day(n):
    return (T + timedelta(days=n)).isoformat()


def submit(client, h, run, **extra):
    return client.post("/events", json={"title": "Odd Nights", "category": "Theater", "venue": VENUE, "run_dates": run, **extra}, headers=h)


def test_submitting_a_fixed_run(client, user_token, admin_token):
    h = auth(user_token)
    r = submit(client, h, [{"date": day(9), "time": "19:30", "end_time": "21:00"}, {"date": day(2), "time": "20:00"}],
               begin_date=day(0), end_date=day(40), open_run=True, weekly={"thu": ["19:30"]})
    assert r.status_code == 201, r.text
    e = r.json()
    assert (e["begin_date"], e["end_date"], e["open_run"]) == (day(2), day(9), False)  # the first and last dates win
    assert e["run_dates"] == [{"date": day(2), "time": "20:00", "end_time": ""}, {"date": day(9), "time": "19:30", "end_time": "21:00"}]
    assert client.get(f"/events/{e['id']}", headers=auth(admin_token)).json()["schedule"]["dates"] == \
        [{"date": day(2), "time": "20:00"}, {"date": day(9), "time": "19:30"}]  # Plan an outing knows the times


def test_checks(client, user_token):
    h = auth(user_token)
    assert submit(client, h, [{"date": day(-1), "time": "20:00"}]).status_code == 422  # in the past
    assert submit(client, h, [{"date": day(1)}]).status_code == 422  # every date needs a time
    assert submit(client, h, [{"date": day(1), "time": "8pm"}]).status_code == 422
    assert submit(client, h, [{"date": day(1), "time": "20:00"}, {"date": day(1), "time": "21:00"}]).status_code == 422  # twice
    assert submit(client, h, [{"date": day(i), "time": "20:00"} for i in range(31)]).status_code == 422  # 30 at most
    assert submit(client, h, [{"date": day(i), "time": "20:00"} for i in range(30)]).status_code == 201
    assert submit(client, h, [{"date": day(0), "time": "20:00"}]).status_code == 201  # today is fine


def test_catalog_matches_only_the_chosen_dates(client, admin_token, make_event):
    h = auth(admin_token)
    assert submit(client, h, [{"date": day(3), "time": "20:00"}, {"date": day(10), "time": "20:00"}], publish="now").status_code == 201
    make_event("Every Night", begin=T, end=T + timedelta(days=30))
    titles = lambda a, b: sorted(e["title"] for e in client.get("/events", params={"from": day(a), "to": day(b)}).json()["items"])
    assert titles(3, 3) == ["Every Night", "Odd Nights"]
    assert titles(4, 9) == ["Every Night"]  # between its dates: not on
    assert titles(8, 12) == ["Every Night", "Odd Nights"]
    assert titles(0, 2) == ["Every Night"]
    assert sorted(e["title"] for e in client.get("/events", params={"from": day(11)}).json()["items"]) == ["Every Night"]  # its run is over


def test_admins_edit_the_dates(client, user_token, admin_token):
    e = submit(client, auth(user_token), [{"date": day(2), "time": "20:00"}, {"date": day(5), "time": "20:00"}]).json()
    a = auth(admin_token)
    r = client.patch(f"/events/{e['id']}", json={"run_dates": [{"date": day(5), "time": "19:00"}, {"date": day(12), "time": "19:00"}]}, headers=a)
    assert r.status_code == 200, r.text
    assert (r.json()["begin_date"], r.json()["end_date"], [d["date"] for d in r.json()["run_dates"]]) == (day(5), day(12), [day(5), day(12)])
    assert client.patch(f"/events/{e['id']}", json={"run_dates": [{"date": day(-3), "time": "19:00"}]}, headers=a).status_code == 422
    assert client.patch(f"/events/{e['id']}", json={"run_dates": None}, headers=a).status_code == 422
    r = client.patch(f"/events/{e['id']}", json={"run_dates": []}, headers=a)  # no longer a fixed run: its dates stay as a range
    assert r.json()["run_dates"] == [] and (r.json()["begin_date"], r.json()["end_date"]) == (day(5), day(12))
    assert client.patch(f"/events/{e['id']}", json={"run_dates": [{"date": day(1), "time": "19:00"}]}, headers=auth(user_token)).status_code == 403


def test_a_past_date_it_already_had_can_stay(client, admin_token, db):
    from app.models import Event, EventRunDate
    e = submit(client, auth(admin_token), [{"date": day(2), "time": "20:00"}], publish="now").json()
    ev = db.get(Event, e["id"])
    ev.run_dates.append(EventRunDate(date=T - timedelta(days=3), time="20:00"))
    db.commit()
    r = client.patch(f"/events/{e['id']}", json={"run_dates": [{"date": day(-3), "time": "20:00"}, {"date": day(4), "time": "20:00"}]},
                     headers=auth(admin_token))
    assert r.status_code == 200, r.text
