"""CC-68: cookie-free traffic counts: views, unique and returning visitors, ticket clicks, outings."""
import json
from datetime import timedelta

from sqlalchemy import select

from app.models import TrafficVisitor, TrafficVisitorDay
from app.stats import today_in_chicago

from .conftest import auth

TODAY = today_in_chicago()
BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh) Safari/605.1", "Content-Type": "text/plain"}
VID_A, VID_B = "a" * 32, "b1b2b3b4-c5c6-d7d8-e9e0-f1f2f3f4f5f6"


def ping(client, headers=BROWSER, **body):
    return client.post("/traffic", content=json.dumps(body), headers=headers)


def summary(client, admin_token, **params):
    r = client.get("/admin/stats/traffic", params=params, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    return r.json()


def test_counts_views_visitors_tickets_and_outings(client, admin_token):
    for body in ({"view": "home", "vid": VID_A}, {"view": "catalog", "vid": VID_A}, {"view": "catalog", "vid": VID_B},
                 {"kind": "ticket", "vid": VID_A}, {"kind": "outing", "vid": VID_B}, {"view": "home"}):
        assert ping(client, **body).status_code == 204
    s = summary(client, admin_token, days=7)
    today = s["days"][-1]
    assert (today["views"], today["visitors"], today["returning"], today["tickets"], today["outings"]) == (4, 3, 0, 1, 1)
    assert s["by_view"] == {"catalog": 2, "home": 2}
    assert s["visitors"] == 3 and len(s["days"]) == 7


def test_returning_visitors(client, db, admin_token):
    ping(client, view="home", vid=VID_A)
    # pretend that browser first came three days ago
    v = db.scalar(select(TrafficVisitor)); v.first_day = TODAY - timedelta(days=3)
    db.add(TrafficVisitorDay(day=TODAY - timedelta(days=3), visitor=v.visitor)); db.commit()
    ping(client, view="home", vid=VID_B)  # new today
    s = summary(client, admin_token, days=7)
    assert (s["days"][-1]["visitors"], s["days"][-1]["returning"]) == (2, 1)
    assert s["days"][-4]["visitors"] == 1 and (s["visitors"], s["returning_visitors"]) == (2, 1)


def test_nothing_identifying_is_stored(client, db):
    ping(client, view="home", vid=VID_A)
    ping(client, view="home")  # no browser ID: counted for today only, never as returning
    stored = [r.visitor for r in db.scalars(select(TrafficVisitorDay))]
    assert len(stored) == 2 and all(VID_A not in v and "Mozilla" not in v and "testclient" not in v for v in stored)
    assert db.scalar(select(TrafficVisitor.visitor)) in stored  # only the ID-based one lasts
    assert sum(v.startswith("d:") for v in stored) == 1


def test_bots_and_junk_are_ignored_quietly(client, admin_token):
    assert ping(client, headers={**BROWSER, "User-Agent": "Googlebot/2.1"}, view="home").status_code == 204
    assert ping(client, view="not-a-view").status_code == 204
    assert client.post("/traffic", content=b"not json", headers=BROWSER).status_code == 204
    assert ping(client, view="home", vid="short").status_code == 204  # a malformed ID: ignored
    assert summary(client, admin_token, days=1)["days"][-1]["views"] == 0


def test_summary_is_admins_only(client, user_token):
    assert client.get("/admin/stats/traffic", headers=auth(user_token)).status_code == 403
    assert client.get("/admin/stats/traffic").status_code == 401
