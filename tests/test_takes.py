"""CC-112, CC-172: takes: a member shares a journal entry's three words and recommendation. Off by default, per entry;
unsharing or un-marking takes it down; an admin can hide one without touching the entry. Who sees one: everyone sees the
admin's ("Yours truly"); a member's shows only to the members of a crew they're in; reviews only to admins."""
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


def crewmate(client, h, name="Ben K", email="ben@example.com"):
    """A second member in a crew with h."""
    token = client.post(f"/crews/{client.post('/crews', json={'name': 'Thursday Theater'}, headers=h).json()['id']}/invites",
                        headers=h).json()["token"]
    mate = member(client, name, email)
    assert client.post("/crews/join", json={"token": token}, headers=mate).status_code in (200, 201)
    return mate


def test_private_unless_shared(client, make_event):
    h, hamlet = member(client), make_event("Hamlet")
    been(client, h, hamlet)
    journal(client, h, hamlet, words="Secretword big bold", review="Privatereview", recommend=True)
    row = client.get("/me/attended", headers=h).json()[0]
    assert (row["shared"], row["anonymous"]) == (False, False)  # off by default
    assert takes(client, hamlet) == [] and takes(client, hamlet, h) == []
    assert "Secretword" not in page(client, hamlet) and "Privatereview" not in page(client, hamlet)


def test_share_shows_to_crewmates_only(client, make_event):
    h, hamlet, other = member(client), make_event("Hamlet"), make_event("Macbeth")
    mate = crewmate(client, h)
    stranger = member(client, "Cy Q", "cy@example.com")
    been(client, h, hamlet)
    assert share(client, h, hamlet).status_code == 422  # the three words come first
    journal(client, h, hamlet, words="moody dark grand", review="Loved <the> ghost.", recommend=True)
    assert share(client, h, hamlet).json() == {"shared": True, "anonymous": False}
    [t] = takes(client, hamlet, mate)
    assert (t["words"], t["recommend"], t["name"], t["by_admin"]) == ("moody dark grand", True, "Maya C.", False)
    assert t["review"] == ""  # the review isn't shown to crew-mates
    assert t["user_id"] is None and t["hidden"] is False  # never whose it is, except to admins
    assert takes(client, hamlet) == [] and takes(client, hamlet, stranger) == []  # signed out, or in no shared crew
    assert takes(client, hamlet, h) == []  # not your own: it's in your journal
    assert takes(client, other, mate) == []
    html = page(client, hamlet)
    assert "moody" not in html and "Loved" not in html and "maya@example.com" not in html  # the public page: not a member's
    journal(client, h, hamlet, words="moody dark grander")  # an edit shows too
    assert takes(client, hamlet, mate)[0]["words"] == "moody dark grander"
    assert client.get("/me/attended", headers=h).json()[0]["shared"] is True


def test_the_admins_take_shows_to_everyone(client, make_event, admin_token):
    admin, h, hamlet = auth(admin_token), member(client), make_event("Hamlet")
    been(client, admin, hamlet)
    journal(client, admin, hamlet, words="grand dark moody", review="Adminreview", recommend=True)
    share(client, admin, hamlet)
    for viewer in (None, h):  # signed out, and in no crew with the admin
        [t] = takes(client, hamlet, viewer)
        assert (t["words"], t["recommend"], t["by_admin"], t["review"]) == ("grand dark moody", True, True, "")
    html = page(client, hamlet)
    assert "<span>grand</span>" in html and "Would recommend" in html and "Adminreview" not in html
    assert takes(client, hamlet, admin)[0]["review"] == "Adminreview"  # admins see reviews, to moderate


def test_anonymous_and_names(client, make_event, admin_token):
    hamlet = make_event("Hamlet")
    for name, email, shown in (("Maya Chen", "maya@example.com", "A Culture Crew member"), ("Cher", "cher@example.com", "Cher"),
                               ("Jo van der berg", "jo@example.com", "Jo B.")):
        h = member(client, name, email)
        been(client, h, hamlet)
        journal(client, h, hamlet, words="one two three")
        share(client, h, hamlet, anonymous=name == "Maya Chen")
    assert [t["name"] for t in takes(client, hamlet, auth(admin_token))] == ["Jo B.", "Cher", "A Culture Crew member"]  # newest first
    assert "Maya" not in page(client, hamlet)


def test_unshare_and_unmark_take_it_down(client, make_event):
    h, hamlet = member(client), make_event("Hamlet")
    mate = crewmate(client, h)
    been(client, h, hamlet)
    journal(client, h, hamlet, words="moody dark grand")
    share(client, h, hamlet)
    assert len(takes(client, hamlet, mate)) == 1
    assert client.put(f"/me/attended/{hamlet.id}/share", json={"shared": False}, headers=h).json()["shared"] is False
    assert takes(client, hamlet, mate) == [] and "moody" not in page(client, hamlet)
    assert client.get("/me/attended", headers=h).json()[0]["words"] == "moody dark grand"  # the entry is kept
    share(client, h, hamlet)
    assert client.delete(f"/me/attended/{hamlet.id}", headers=h).status_code == 204
    assert takes(client, hamlet, mate) == [] and "moody" not in page(client, hamlet)
    been(client, h, hamlet)  # marking again starts private
    assert client.get("/me/attended", headers=h).json()[0]["shared"] is False and takes(client, hamlet, mate) == []


def test_only_your_own(client, make_event):
    h, other, hamlet = member(client), member(client, "Ben K", "ben@example.com"), make_event("Hamlet")
    assert share(client, h, hamlet).status_code == 404  # not been
    been(client, h, hamlet)
    journal(client, h, hamlet, words="moody dark grand")
    assert share(client, other, hamlet).status_code == 404  # Ben can't share Maya's
    assert client.put(f"/me/attended/{hamlet.id}/share", json={"shared": True}).status_code == 401


def test_admin_hides_without_deleting(client, make_event, admin_token):
    h, admin, hamlet = member(client), auth(admin_token), make_event("Hamlet")
    mate = crewmate(client, h)
    been(client, h, hamlet)
    journal(client, h, hamlet, words="rude words here")
    share(client, h, hamlet)
    [t] = takes(client, hamlet, admin)
    assert t["user_id"] and t["hidden"] is False  # admins see whose it is, to hide it
    url = f"/admin/takes/{hamlet.id}/{t['user_id']}"
    assert client.put(url, json={"hidden": True}, headers=h).status_code == 403  # admins only
    assert client.put(url, json={"hidden": True}, headers=admin).status_code == 204
    assert takes(client, hamlet, mate) == [] and "rude" not in page(client, hamlet)
    assert takes(client, hamlet, admin)[0]["hidden"] is True  # admins still see it, marked
    row = client.get("/me/attended", headers=h).json()[0]
    assert row["words"] == "rude words here" and row["shared"] is True and "hidden" not in row  # the member isn't told
    journal(client, h, hamlet, words="still rude words")  # an edit or re-share doesn't bring it back
    client.put(f"/me/attended/{hamlet.id}/share", json={"shared": False}, headers=h)
    share(client, h, hamlet)
    assert takes(client, hamlet, mate) == []
    assert client.put(url, json={"hidden": False}, headers=admin).status_code == 204
    assert takes(client, hamlet, mate)[0]["words"] == "still rude words"
    client.put(f"/me/attended/{hamlet.id}/share", json={"shared": False}, headers=h)
    assert client.put(url, json={"hidden": True}, headers=admin).status_code == 404  # only a shared take
