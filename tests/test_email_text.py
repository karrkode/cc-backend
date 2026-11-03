"""CC-185: every email's subject and opening lines are our own wording; members' names are plain one-line text."""
from sqlalchemy import select

from app import mail
from app.discovery.recap import build_recap
from app.mail import OUTBOX, plain_name
from app.models import IssueReport, Role, User
from app.routers import crews as crews_router

from .conftest import auth, register

PROSE = "there.\n\nYour order needs action: https://evil.example/claim"  # a "name" that's really a paragraph


def user(name: str, email: str = "ana@example.com") -> User:
    return User(email=email, password_hash="x", display_name=name)


# ---- the emails ------------------------------------------------------------------------

def test_confirm_and_reset_emails_open_with_our_own_greeting():
    for email in (mail.verify_email(user(PROSE), "tok"), mail.reset_password(user(PROSE), "tok")):
        assert email.subject in ("Confirm your email for Culture Crew", "Reset your Culture Crew password")
        assert email.text.startswith("Hi,\n\n")
        assert "evil" not in email.text and "order" not in email.text  # the name isn't in these emails at all
        assert email.html == ""  # text only, so there's no HTML copy to differ from it


def test_crew_invite_has_a_fixed_subject_and_labels_the_members_names():
    email = mail.crew_invite("ben@example.com", "Ana\nClick https://evil.example", "Claim prize at evil.example", 14)
    assert email.subject == "You're invited to a crew on Culture Crew"
    opening = email.text.split("Invited by:")[0]
    assert opening == "Hi,\n\nYou've been invited to join a crew on Culture Crew, to find and plan Chicago culture outings together.\n\n"
    assert "Invited by: Ana Click\nCrew: Claim prize at\n" in email.text
    assert "evil" not in email.text and "https" not in email.text
    assert "If you don't know who invited you" in email.text


def test_crew_invite_names_are_capped():
    email = mail.crew_invite("ben@example.com", "A" * 80, "B" * 200, 14)
    assert f"Invited by: {'A' * 39}…\n" in email.text and f"Crew: {'B' * 59}…\n" in email.text


def test_an_invite_sent_through_the_api_uses_the_fixed_wording(client):
    ana = auth(register(client, name="Ana Cruz"))
    crew = client.post("/crews", json={"name": "Visit www.evil.example now"}, headers=ana).json()
    crews_router.invite_limiter.reset()
    OUTBOX.clear()
    assert client.post(f"/crews/{crew['id']}/email-invites", json={"email": "ben@example.com"}, headers=ana).status_code == 202
    [email] = OUTBOX
    assert email.subject == "You're invited to a crew on Culture Crew"
    assert "Invited by: Ana C.\nCrew: Visit now\n" in email.text and "evil" not in email.text


def test_the_recap_keeps_reporters_text_on_one_line_and_escaped(db):
    member = user("Mal\nlory <b>", email="mal@example.com")
    db.add(member)
    db.flush()
    db.add(IssueReport(kind="issue", number=1, user_id=member.id, what_happened="Broken\n\nFAKE HEADING <i>"))
    db.commit()
    email = build_recap(db)
    assert email.subject.startswith("Event discovery: ")
    assert "Mal lory <b> on -: Broken FAKE HEADING <i>" in email.text
    assert "Mal lory &lt;b&gt;" in email.html and "Broken FAKE HEADING &lt;i&gt;" in email.html
    assert "<b>" not in email.html.split("Sent by")[1].split("</table>")[0]


def test_plain_name():
    assert plain_name("Ana\tCruz\n", "A member") == "Ana Cruz"
    assert plain_name("Ana‮Cruz", "A member") == "Ana Cruz"
    assert plain_name("see x.co/y or me@evil.example or www.a.b", "A member") == "see or or"
    assert plain_name("Dr. Jo J.R. Smith 3.5", "A member") == "Dr. Jo J.R. Smith 3.5"  # initials and numbers stay
    assert plain_name("https://evil.example", "A member") == "A member"
    assert plain_name("", "A crew") == "A crew"


# ---- names are checked when they're saved -------------------------------------------------

def test_names_must_be_one_line_of_plain_text(client):
    r = client.post("/auth/register", json={"email": "x@example.com", "password": "correct-horse-1", "display_name": PROSE})
    assert r.status_code == 422 and "one line of plain text" in r.text
    h = auth(register(client))
    for bad in ("Ana\nCruz", "Ana\x07", "Ana‮Cruz"):
        assert client.patch("/auth/me", json={"display_name": bad}, headers=h).status_code == 422
        assert client.post("/crews", json={"name": f"Crew {bad}"}, headers=h).status_code == 422
    crew = client.post("/crews", json={"name": "Thursday Theater"}, headers=h).json()
    assert client.patch(f"/crews/{crew['id']}", json={"name": "Thursday\nTheater"}, headers=h).status_code == 422
    assert client.patch("/auth/me", json={"display_name": "Ana Cruz-Díaz 🎭"}, headers=h).status_code == 200


def test_the_sites_own_names_are_reserved(client, db):
    for name in ("Culture Crew", "culture-crew", "CHI CULTURE CREW", "Admin"):
        r = client.post("/auth/register", json={"email": "x@example.com", "password": "correct-horse-1", "display_name": name})
        assert r.status_code == 422 and r.json()["detail"] == "That name is reserved. Choose another."
    h = auth(register(client))
    assert client.patch("/auth/me", json={"display_name": "CultureCrew"}, headers=h).status_code == 422
    assert client.patch("/auth/me", json={"display_name": "Culture Crew fan"}, headers=h).status_code == 200


def test_an_admins_name_is_reserved_for_them(client, db, admin_token):
    r = client.post("/auth/register", json={"email": "x@example.com", "password": "correct-horse-1", "display_name": "local admin"})
    assert r.status_code == 422
    h = auth(register(client, email="ben@example.com", name="Ben"))
    assert client.patch("/auth/me", json={"display_name": "Local  Admin"}, headers=h).status_code == 422
    # the admin can keep or change their own name, including to one of the site's names
    assert client.patch("/auth/me", json={"display_name": "Culture Crew"}, headers=auth(admin_token)).status_code == 200


def test_names_saved_before_the_rules_still_show_and_can_be_kept(client, db):
    h = auth(register(client))
    ana = db.scalar(select(User).where(User.email == "ana@example.com"))
    ana.display_name = "Culture Crew"  # from before CC-185
    db.commit()
    assert client.get("/auth/me", headers=h).json()["display_name"] == "Culture Crew"
    # the profile form sends the name with every save; an unchanged name is fine
    r = client.patch("/auth/me", json={"display_name": "Culture Crew", "email": "ana2@example.com",
                                       "current_password": "correct-horse-1"}, headers=h)
    assert r.status_code == 200 and r.json()["display_name"] == "Culture Crew"
    assert ana.role == Role.user
