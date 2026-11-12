"""CC-68: cookie-free traffic counts: views, unique and returning visitors, ticket clicks, outings."""
import json
from datetime import timedelta

from sqlalchemy import func, select

from app.config import get_settings
from app.models import TrafficVisitor, TrafficVisitorDay
from app.stats import today_in_chicago
from app.traffic import PingLimits, address_key

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


def test_invite_page_visits_and_sign_ups_by_link_and_qr(client, admin_token):
    # CC-126: the invite page (/invite) is the join page with the invite pitch. An admin's link is tagged invite / link,
    # the code on their phone invite / qr, and each counts on its own row.
    link = {"utm_source": "invite", "utm_medium": "link", "utm_campaign": "friend"}
    qr = {"utm_source": "invite", "utm_medium": "qr", "utm_campaign": "friend"}
    ping(client, view="join", vid=VID_A, **link)
    ping(client, view="join", vid=VID_B, **qr)
    ping(client, view="join", vid="c" * 32, **qr)
    register(client, "scanned@example.com", qr)
    j = summary(client, admin_token, days=7)["join"]
    assert [(r["source"], r["medium"], r["visits"], r["signups"]) for r in j["sources"]] == [
        ("invite", "qr", 2, 1), ("invite", "link", 1, 0)]


# CC-190: a script can't inflate the stats, and the page never notices.
def vid(n):
    return f"{n:032d}"


def from_address(address):
    return {**BROWSER, "X-Forwarded-For": address}


def test_pings_past_the_limit_a_minute_are_ignored(client, admin_token):
    limit = get_settings().traffic_pings_per_minute
    answers = {ping(client, from_address("203.0.113.1"), view="home", vid=VID_A).status_code for _ in range(limit + 5)}
    assert answers == {204}
    assert summary(client, admin_token, days=1)["by_view"] == {"home": limit}


def test_the_limit_a_minute_is_a_sliding_window(client, admin_token, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("app.ratelimit.time.monotonic", lambda: now[0])
    limit = get_settings().traffic_pings_per_minute
    for _ in range(limit + 1):
        ping(client, from_address("203.0.113.1"), view="home", vid=VID_A)
    now[0] += 61
    ping(client, from_address("203.0.113.1"), view="home", vid=VID_A)
    assert summary(client, admin_token, days=1)["by_view"] == {"home": limit + 1}


def test_each_address_has_its_own_limit(client, admin_token):
    limit = get_settings().traffic_pings_per_minute
    for _ in range(limit + 1):
        ping(client, from_address("203.0.113.1"), view="home", vid=VID_A)
    ping(client, from_address("203.0.113.2, 10.0.0.1"), view="home", vid=VID_A)  # the first address is the client's
    assert summary(client, admin_token, days=1)["by_view"] == {"home": limit + 1}


def test_new_visitors_past_the_daily_limit_are_ignored(client, admin_token):
    limit = get_settings().traffic_new_visitors_per_day
    for n in range(limit + 5):
        assert ping(client, from_address("203.0.113.1"), view="home", vid=vid(n), utm_source="probe").status_code == 204
    s = summary(client, admin_token, days=1)
    assert s["visitors"] == limit and s["by_view"] == {"home": limit}
    assert [r["visitors"] for r in s["sources"]] == [limit]

    ping(client, from_address("203.0.113.1"), view="catalog", vid=vid(0))  # someone already counted carries on
    ping(client, from_address("203.0.113.2"), view="home", vid=vid(99))  # and another address is unaffected
    s = summary(client, admin_token, days=1)
    assert s["visitors"] == limit + 1 and s["by_view"] == {"home": limit + 1, "catalog": 1}


def test_visitors_without_a_browser_id_count_toward_the_daily_limit(client, admin_token):
    limit = get_settings().traffic_new_visitors_per_day
    for n in range(limit + 5):
        ping(client, {**from_address("203.0.113.1"), "User-Agent": f"Mozilla/5.0 Safari/{n}"}, view="home")
    assert summary(client, admin_token, days=1)["visitors"] == limit


def test_the_limits_start_again_each_day():
    limits, today = PingLimits(), TODAY
    allowed = get_settings().traffic_new_visitors_per_day
    assert all(limits.visitor_allowed(today, "a", str(n)) for n in range(allowed))
    assert not limits.visitor_allowed(today, "a", "one more")
    assert limits.visitor_allowed(today + timedelta(days=1), "a", "one more")


def test_the_limits_keep_no_address():
    assert "203.0.113.1" not in address_key(TODAY, "203.0.113.1")
    assert address_key(TODAY, "203.0.113.1") != address_key(TODAY + timedelta(days=1), "203.0.113.1")


def test_visitor_counts_survive_a_sign_in_key_change(client, db, monkeypatch):  # CC-204
    monkeypatch.setattr(get_settings(), "visitor_hash_secret", "v" * 40)
    ping(client, view="home", vid=VID_A)
    v = db.scalar(select(TrafficVisitor)); v.first_day = TODAY - timedelta(days=3)
    db.add(TrafficVisitorDay(day=TODAY - timedelta(days=3), visitor=v.visitor)); db.commit()
    monkeypatch.setattr(get_settings(), "jwt_secret", "rotated-sign-in-key-of-32-characters!")
    ping(client, view="home", vid=VID_A)
    # the same browser is still the same visitor (the old admin token no longer signs in, so look in the tables)
    assert db.scalar(select(func.count()).select_from(TrafficVisitor)) == 1
    assert db.scalar(select(func.count()).select_from(TrafficVisitorDay).where(TrafficVisitorDay.day == TODAY)) == 1
