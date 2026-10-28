"""CC-112: public takes: a member shares a journal entry's three words, review and recommendation on the event's
page and in the site's event view. Off by default, per entry; unsharing or un-marking takes it down; an admin can
hide one without touching the entry."""
from .conftest import auth, register
from .test_journal import been, journal


def share(client, h, ev, **body):
    return client.put(f"/me/attended/{ev.id}/share", json={"shared": True, **body}, headers=h)


def takes(client, ev, h=None):
    return client.get(f"/events/{ev.id}", headers=h or {}).json()["takes"]


def page(client, ev):
    return client.get(f"/e/{ev.id}-{ev.title.lower()}").text


def member(client, name="Maya Chen", email="maya@example.com"):
    return auth(register(client, email=email, name=name))


def test_private_unless_shared(client, make_event):
    h, hamlet = member(client), make_event("Hamlet")
    been(client, h, hamlet)
    journal(client, h, hamlet, words="Secretword big bold", review="Privatereview", recommend=True)
    row = client.get("/me/attended", headers=h).json()[0]
    assert (row["shared"], row["anonymous"]) == (False, False)  # off by default
    assert takes(client, hamlet) == [] and takes(client, hamlet, h) == []
    assert "Secretword" not in page(client, hamlet) and "Privatereview" not in page(client, hamlet)


def test_share_shows_on_the_event_and_its_page(client, make_event):
    h, hamlet, other = member(client), make_event("Hamlet"), make_event("Macbeth")
    been(client, h, hamlet)
    assert share(client, h, hamlet).status_code == 422  # the three words come first
    journal(client, h, hamlet, words="moody dark grand", review="Loved <the> ghost.", recommend=True)
    assert share(client, h, hamlet).json() == {"shared": True, "anonymous": False}
    [t] = takes(client, hamlet)  # signed out too
    assert (t["words"], t["review"], t["recommend"], t["name"]) == ("moody dark grand", "Loved <the> ghost.", True, "Maya C.")
    assert t["user_id"] is None and t["hidden"] is False  # never whose it is, except to admins
    assert takes(client, other) == []
    html = page(client, hamlet)
    assert "From people who went" in html and "<span>moody</span>" in html and "Maya C." in html
    assert "Loved &lt;the&gt; ghost." in html and "Would recommend" in html and "maya@example.com" not in html
    journal(client, h, hamlet, words="moody dark grander")  # an edit shows too
    assert takes(client, hamlet)[0]["words"] == "moody dark grander"
    assert client.get("/me/attended", headers=h).json()[0]["shared"] is True


def test_anonymous_and_names(client, make_event):
    hamlet = make_event("Hamlet")
    for name, email, shown in (("Maya Chen", "maya@example.com", "A Culture Crew member"), ("Cher", "cher@example.com", "Cher"),
                               ("Jo van der berg", "jo@example.com", "Jo B.")):
        h = member(client, name, email)
        been(client, h, hamlet)
        journal(client, h, hamlet, words="one two three")
        share(client, h, hamlet, anonymous=name == "Maya Chen")
    assert [t["name"] for t in takes(client, hamlet)] == ["Jo B.", "Cher", "A Culture Crew member"]  # newest first
    assert "Maya" not in page(client, hamlet)


def test_unshare_and_unmark_take_it_down(client, make_event):
    h, hamlet = member(client), make_event("Hamlet")
    been(client, h, hamlet)
    journal(client, h, hamlet, words="moody dark grand")
    share(client, h, hamlet)
    assert client.put(f"/me/attended/{hamlet.id}/share", json={"shared": False}, headers=h).json()["shared"] is False
    assert takes(client, hamlet) == [] and "moody" not in page(client, hamlet)
    assert client.get("/me/attended", headers=h).json()[0]["words"] == "moody dark grand"  # the entry is kept
    share(client, h, hamlet)
    assert client.delete(f"/me/attended/{hamlet.id}", headers=h).status_code == 204
    assert takes(client, hamlet) == [] and "moody" not in page(client, hamlet)
    been(client, h, hamlet)  # marking again starts private
    assert client.get("/me/attended", headers=h).json()[0]["shared"] is False and takes(client, hamlet) == []


def test_only_your_own(client, make_event):
    h, other, hamlet = member(client), member(client, "Ben K", "ben@example.com"), make_event("Hamlet")
    assert share(client, h, hamlet).status_code == 404  # not been
    been(client, h, hamlet)
    journal(client, h, hamlet, words="moody dark grand")
    assert share(client, other, hamlet).status_code == 404  # Ben can't share Maya's
    assert client.put(f"/me/attended/{hamlet.id}/share", json={"shared": True}).status_code == 401


def test_admin_hides_without_deleting(client, make_event, admin_token):
    h, admin, hamlet = member(client), auth(admin_token), make_event("Hamlet")
    been(client, h, hamlet)
    journal(client, h, hamlet, words="rude words here")
    share(client, h, hamlet)
    [t] = takes(client, hamlet, admin)
    assert t["user_id"] and t["hidden"] is False  # admins see whose it is, to hide it
    url = f"/admin/takes/{hamlet.id}/{t['user_id']}"
    assert client.put(url, json={"hidden": True}, headers=h).status_code == 403  # admins only
    assert client.put(url, json={"hidden": True}, headers=admin).status_code == 204
    assert takes(client, hamlet) == [] and takes(client, hamlet, h) == [] and "rude" not in page(client, hamlet)
    assert takes(client, hamlet, admin)[0]["hidden"] is True  # admins still see it, marked
    row = client.get("/me/attended", headers=h).json()[0]
    assert row["words"] == "rude words here" and row["shared"] is True and "hidden" not in row  # the member isn't told
    journal(client, h, hamlet, words="still rude words")  # an edit or re-share doesn't bring it back
    client.put(f"/me/attended/{hamlet.id}/share", json={"shared": False}, headers=h)
    share(client, h, hamlet)
    assert takes(client, hamlet) == []
    assert client.put(url, json={"hidden": False}, headers=admin).status_code == 204
    assert takes(client, hamlet)[0]["words"] == "still rude words"
    client.put(f"/me/attended/{hamlet.id}/share", json={"shared": False}, headers=h)
    assert client.put(url, json={"hidden": True}, headers=admin).status_code == 404  # only a shared take
