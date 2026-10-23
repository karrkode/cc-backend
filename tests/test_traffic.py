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


# ---- CC-110: where visitors come from ----

def test_tagged_link_counts_under_its_source_and_campaign(client, admin_token):
    tags = {"utm_source": "facebook", "utm_medium": "paid", "utm_campaign": "weekend"}
    ping(client, view="home", vid=VID_A, **tags)
    ping(client, view="catalog", vid=VID_A, **tags)  # the same visitor again: still one
    ping(client, kind="ticket", vid=VID_A, **tags)
    s = summary(client, admin_token, days=7)
    assert s["sources"] == [{"source": "facebook", "medium": "paid", "campaign": "weekend", "visitors": 1, "tickets": 1}]
    assert s["direct"] == {"visitors": 0, "tickets": 0}


def test_referring_site_counts_by_its_domain(client, admin_token):
    ping(client, view="home", vid=VID_A, ref="www.google.com")
    ping(client, view="home", vid=VID_B, ref="google.co.uk")  # another Google: the same search engine
    ping(client, view="home", ref="l.facebook.com")  # Facebook's link wrapper is Facebook
    s = summary(client, admin_token, days=7)
    assert [(r["source"], r["medium"], r["visitors"]) for r in s["sources"]] == [("google.com", "site", 2), ("facebook.com", "site", 1)]


def test_tags_win_over_the_referring_site(client, admin_token):
    ping(client, view="home", vid=VID_A, utm_source="Instagram", utm_campaign="Last Chance!", ref="l.instagram.com")
    s = summary(client, admin_token, days=7)
    assert [(r["source"], r["medium"], r["campaign"]) for r in s["sources"]] == [("instagram", "", "last-chance")]


def test_direct_visits_and_tickets_are_whats_left(client, admin_token):
    ping(client, view="home", vid=VID_A, utm_source="qr", utm_campaign="card")
    ping(client, kind="ticket", vid=VID_A, utm_source="qr", utm_campaign="card")
    ping(client, view="home", vid=VID_B)
    ping(client, kind="ticket", vid=VID_B)
    ping(client, kind="ticket", vid=VID_B)
    s = summary(client, admin_token, days=7)
    assert [(r["source"], r["visitors"], r["tickets"]) for r in s["sources"]] == [("qr", 1, 1)]
    assert s["direct"] == {"visitors": 1, "tickets": 2}


def test_our_own_site_and_bad_referrers_are_not_sources(client, admin_token):
    for ref in ("chiculturecrew.com", "www.chiculturecrew.com", "api.chiculturecrew.com", "localhost",
                "https://google.com/search?q=secret", "not a domain"):
        ping(client, view="home", vid=VID_A, ref=ref)
    s = summary(client, admin_token, days=7)
    assert s["sources"] == [] and s["direct"]["visitors"] == 1


def test_only_the_domain_is_stored(client, db):
    from app.models import TrafficSourceVisit
    ping(client, view="home", vid=VID_A, ref="news.example.org")
    ping(client, view="home", vid=VID_B, ref="news.example.org/story?id=7")  # a path or query: refused
    rows = db.scalars(select(TrafficSourceVisit)).all()
    assert [(r.source, r.medium, r.campaign) for r in rows] == [("news.example.org", "site", "")]
    assert VID_A not in rows[0].visitor
