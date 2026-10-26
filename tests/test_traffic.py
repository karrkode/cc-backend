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


# ---- CC-121: the join page: visits and sign-ups by source ----
def register(client, email, join_source=None):
    body = {"email": email, "password": "join-page-test-pw", "display_name": "Joiner"}
    if join_source is not None:
        body["join_source"] = join_source
    r = client.post("/auth/register", json=body)
    assert r.status_code == 201, r.text
    return r


QR = {"utm_source": "qr", "utm_medium": "", "utm_campaign": "lobby-goodman"}


def test_a_join_page_visit_that_signs_up_counts_under_its_source(client, admin_token):
    ping(client, view="join", vid=VID_A, **QR)
    register(client, "goodman@example.com", QR)
    j = summary(client, admin_token, days=7)["join"]
    assert j["sources"] == [{"source": "qr", "medium": "", "campaign": "lobby-goodman", "visits": 1, "signups": 1, "rate": 1.0}]
    assert (j["visits"], j["signups"], j["rate"]) == (1, 1, 1.0)


def test_join_rate_by_source_and_direct(client, admin_token):
    for vid in (VID_A, VID_B, "c" * 32, "d" * 32):
        ping(client, view="join", vid=vid, utm_source="bookclub", utm_campaign="oct")
    ping(client, view="join", vid="e" * 32)  # typed it in: direct
    ping(client, view="join", vid=VID_A, utm_source="bookclub", utm_campaign="oct")  # same visitor, same day: one visit
    register(client, "one@example.com", {"utm_source": "bookclub", "utm_campaign": "oct"})
    register(client, "two@example.com", {})  # came to /join with no source
    j = summary(client, admin_token, days=7)["join"]
    assert [(r["source"], r["campaign"], r["visits"], r["signups"], r["rate"]) for r in j["sources"]] == [("bookclub", "oct", 4, 1, 0.25)]
    assert j["direct"] == {"visits": 1, "signups": 1, "rate": 1.0}
    assert (j["visits"], j["signups"], j["rate"]) == (5, 2, 0.4)


def test_a_sign_up_elsewhere_is_not_a_join_page_sign_up(client, admin_token):
    ping(client, view="home", vid=VID_A, **QR)  # a tagged visit to Home, not the join page
    register(client, "elsewhere@example.com")
    j = summary(client, admin_token, days=7)["join"]
    assert (j["visits"], j["signups"], j["sources"], j["direct"]["signups"]) == (0, 0, [], 0)
    assert j["rate"] is None


def test_join_counts_keep_nothing_identifying(client, db):
    from app.models import TrafficJoinSignup, TrafficJoinVisit
    ping(client, view="join", vid=VID_A, **QR)
    register(client, "private.person@example.com", QR)
    stored = [tuple(getattr(r, c.name) for c in r.__table__.columns) for model in (TrafficJoinVisit, TrafficJoinSignup)
              for r in db.scalars(select(model))]
    text = repr(stored)
    assert len(stored) == 2 and "private" not in text and "Joiner" not in text and VID_A not in text and "testclient" not in text


def test_join_tracking_doesnt_get_in_the_way_of_signing_up(client):
    r = register(client, "junk@example.com", {"utm_source": "<script>", "ref": "not a domain"})
    assert r.json()["access_token"]
