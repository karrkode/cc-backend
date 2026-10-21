"""Culture crews (CC-74): people who go out together. A member can be in several crews; any member can
make an invite link; a crew has up to 25 members. Members see each other's names, never emails, and
nobody outside a crew can see who's in it (a crew you're not in is "not found").

Invite links carry a random token in the URL fragment (#join=…), which browsers never send to a server.
Only its hash is stored; a link lasts 7 days and can be revoked by whoever made it, or by the owner.
"""
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response, status
from sqlalchemy import func, select

from .. import mail
from ..config import get_settings
from ..crewplans import crew_board, unshare, unshare_been
from ..deps import DB, CurrentUser, OptionalUser
from ..models import Crew, CrewEmailInvite, CrewInvite, CrewMember, User
from ..ratelimit import SlidingWindowLimiter
from ..schemas import (CrewBoardItem, CrewEmailInviteOut, CrewIn, EmailInviteIn, PendingInviteOut, CrewInviteLink, CrewInviteOut, CrewInvitePreview, CrewMemberOut, CrewOut, CrewPatch,
                       JoinIn, OwnerIn)
from ..security import hash_refresh_token as hash_token
from ..stats import today_in_chicago
from .approvals import short_name

router = APIRouter(prefix="/crews", tags=["crews"])

MAX_MEMBERS = 25
MAX_OWNED = 10  # crews one person can start
INVITE_DAYS = 7
EMAIL_INVITE_DAYS = 14  # CC-59: long enough for someone to get round to registering
INVITES_PER_HOUR = 20
INVITE_EMAILS_PER_ADDRESS_PER_DAY = 3  # CC-83: from all crews together, so one inbox can't be flooded
invite_limiter = SlidingWindowLimiter(window_seconds=3600)


def name_of(user: User | None) -> str:
    return (user.display_name.strip() if user else "") or "A member without a name"


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)  # SQLite hands back naive UTC


def _live(invite: CrewInvite) -> bool:
    return invite.revoked_at is None and _aware(invite.expires_at) > datetime.now(UTC)


def _member_crew(db: DB, crew_id: int, user: User) -> Crew:
    """The crew, if this user is in it. To anyone else it doesn't exist."""
    crew = db.get(Crew, crew_id)
    if crew is None or not any(m.user_id == user.id for m in crew.members):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Crew not found")
    return crew


def _owned_crew(db: DB, crew_id: int, user: User) -> Crew:
    crew = _member_crew(db, crew_id, user)
    if crew.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the crew leader can do that")
    return crew


def _out(db: DB, crew: Crew, user: User, board: dict | None = None) -> CrewOut:
    if board is None:
        board = crew_board(db, {crew.id}, user, today_in_chicago())
    invites = db.scalars(select(CrewInvite).where(CrewInvite.crew_id == crew.id).order_by(CrewInvite.created_at.desc())).all()
    return CrewOut(
        id=crew.id, name=crew.name, description=crew.description, owner=crew.owner_id == user.id, max_members=MAX_MEMBERS,
        members=[CrewMemberOut(user_id=m.user_id, name=name_of(m.user), owner=m.user_id == crew.owner_id, you=m.user_id == user.id,
                               joined_at=m.joined_at)
                 for m in sorted(crew.members, key=lambda m: (m.user_id != crew.owner_id, _aware(m.joined_at), name_of(m.user)))],
        invites=[CrewInviteOut(id=i.id, made_by=name_of(i.creator), yours=i.created_by == user.id, created_at=i.created_at,
                               expires_at=i.expires_at, uses=i.uses) for i in invites if _live(i)],
        plans=[CrewBoardItem(event=row["event"], people=row["people"], been=row["been"]) for row in board.get(crew.id, [])],
        email_invites=[CrewEmailInviteOut(id=i.id, made_by=name_of(i.inviter), yours=i.invited_by == user.id,
                                          email=i.email if i.invited_by == user.id else None, expires_at=i.expires_at)
                       for i in db.scalars(select(CrewEmailInvite).where(CrewEmailInvite.crew_id == crew.id)
                                           .order_by(CrewEmailInvite.created_at)) if _aware(i.expires_at) > datetime.now(UTC)])


@router.get("", response_model=list[CrewOut])
def my_crews(db: DB, user: CurrentUser):
    """Your crews, oldest first, with their members and live invite links."""
    crews = db.scalars(select(Crew).join(CrewMember).where(CrewMember.user_id == user.id).order_by(Crew.created_at, Crew.id)).all()
    board = crew_board(db, {c.id for c in crews}, user, today_in_chicago())  # CC-75: each crew's shared plans
    return [_out(db, c, user, board) for c in crews]


@router.post("", response_model=CrewOut, status_code=status.HTTP_201_CREATED)
def start_crew(body: CrewIn, db: DB, user: CurrentUser):
    owned = db.scalar(select(func.count()).select_from(Crew).where(Crew.owner_id == user.id))
    if owned >= MAX_OWNED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"You can start up to {MAX_OWNED} crews. Hand one over or dissolve one first.")
    crew = Crew(name=body.name, description=body.description, owner_id=user.id)
    crew.members.append(CrewMember(user_id=user.id))
    db.add(crew)
    db.commit()
    db.refresh(crew)
    return _out(db, crew, user)


@router.patch("/{crew_id}", response_model=CrewOut)
def edit_crew(crew_id: int, body: CrewPatch, db: DB, user: CurrentUser):
    crew = _owned_crew(db, crew_id, user)
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Give the crew a name")
        crew.name = body.name.strip()
    if body.description is not None:
        crew.description = body.description.strip()
    db.commit()
    return _out(db, crew, user)


@router.delete("/{crew_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_crew(crew_id: int, db: DB, user: CurrentUser):
    db.delete(_owned_crew(db, crew_id, user))
    db.commit()


@router.post("/{crew_id}/invites", response_model=CrewInviteLink, status_code=status.HTTP_201_CREATED)
def make_invite(crew_id: int, db: DB, user: CurrentUser):
    """A new invite link, for any member. Rate limited."""
    crew = _member_crew(db, crew_id, user)
    if len(crew.members) >= MAX_MEMBERS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This crew is full ({MAX_MEMBERS} members).")
    if not invite_limiter.hit(str(user.id), INVITES_PER_HOUR):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's a lot of invite links. Try again in an hour.")
    raw = secrets.token_urlsafe(24)
    invite = CrewInvite(crew_id=crew.id, token_hash=hash_token(raw), created_by=user.id,
                        expires_at=datetime.now(UTC) + timedelta(days=INVITE_DAYS))
    db.add(invite)
    db.commit()
    return CrewInviteLink(id=invite.id, token=raw, url=f"{get_settings().site_url.rstrip('/')}/#join={raw}",
                          expires_at=invite.expires_at)


@router.delete("/{crew_id}/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invite(crew_id: int, invite_id: int, db: DB, user: CurrentUser):
    """Whoever made the link, or the owner, can revoke it."""
    crew = _member_crew(db, crew_id, user)
    invite = db.get(CrewInvite, invite_id)
    if invite is None or invite.crew_id != crew.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    if invite.created_by != user.id and crew.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only whoever made this link, or the crew leader, can revoke it")
    invite.revoked_at = invite.revoked_at or datetime.now(UTC)
    db.commit()


def _invite(db: DB, token: str) -> CrewInvite:
    invite = db.scalar(select(CrewInvite).where(CrewInvite.token_hash == hash_token(token)))
    if invite is None or not _live(invite):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This invite link has expired or was turned off. Ask for a new one.")
    return invite


@router.get("/invite/{token}", response_model=CrewInvitePreview)
def preview_invite(token: str, db: DB, user: OptionalUser):
    """What the link is for, before signing in or joining: the crew's name, who invited you, how many are in it."""
    invite = _invite(db, token)
    crew = db.get(Crew, invite.crew_id)
    return CrewInvitePreview(crew_id=crew.id, crew_name=crew.name, description=crew.description, invited_by=name_of(invite.creator),
                             members=len(crew.members), already_member=bool(user) and any(m.user_id == user.id for m in crew.members),
                             full=len(crew.members) >= MAX_MEMBERS)


@router.post("/join", response_model=CrewOut)
def join(body: JoinIn, db: DB, user: CurrentUser, response: Response):
    invite = _invite(db, body.token)
    crew = db.get(Crew, invite.crew_id)
    if any(m.user_id == user.id for m in crew.members):
        return _out(db, crew, user)  # already in: fine
    if len(crew.members) >= MAX_MEMBERS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This crew is full ({MAX_MEMBERS} members).")
    crew.members.append(CrewMember(user_id=user.id))
    invite.uses += 1
    db.commit()
    db.refresh(crew)
    response.status_code = status.HTTP_201_CREATED
    return _out(db, crew, user)


@router.post("/{crew_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave(crew_id: int, db: DB, user: CurrentUser):
    """Leave a crew. The owner hands it to someone first; an owner who's the only member deletes it instead."""
    crew = _member_crew(db, crew_id, user)
    if crew.owner_id == user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You lead this crew. Hand it to another member first, or dissolve it.")
    crew.members = [m for m in crew.members if m.user_id != user.id]
    unshare(db, user.id, crew_id=crew.id)  # CC-75: your plans leave with you
    unshare_been(db, user.id, crew_id=crew.id)  # CC-93: and your "I've been" marks
    db.commit()


@router.delete("/{crew_id}/members/{user_id}", response_model=CrewOut)
def remove_member(crew_id: int, user_id: uuid.UUID, db: DB, user: CurrentUser):
    crew = _owned_crew(db, crew_id, user)
    if user_id == user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You can't remove yourself. Hand the crew over, or dissolve it.")
    if not any(m.user_id == user_id for m in crew.members):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a member of this crew")
    crew.members = [m for m in crew.members if m.user_id != user_id]
    unshare(db, user_id, crew_id=crew.id)
    unshare_been(db, user_id, crew_id=crew.id)
    db.commit()
    return _out(db, crew, user)


@router.post("/{crew_id}/owner", response_model=CrewOut)
def hand_over(crew_id: int, body: OwnerIn, db: DB, user: CurrentUser):
    crew = _owned_crew(db, crew_id, user)
    if not any(m.user_id == body.user_id for m in crew.members) or body.user_id == user.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose another member of this crew")
    crew.owner_id = body.user_id
    db.commit()
    return _out(db, crew, user)


# ---- invites by email address (CC-59) --------------------------------------------------
# Any member can invite an address. It shows to the account with that address on sign-in (now, or once someone
# registers with it within 14 days). CC-83: it's also emailed, once per address per crew while it waits. The
# inviter is never told whether the address has an account, or whether that person is already in the crew.
SENT = "Invite sent. We've emailed them, and they'll see it when they sign in."


def _sees_invites(user: User) -> bool:
    """With require_verified_email on (in production, CC-46), only a confirmed address sees them."""
    return user.email_verified or not get_settings().require_verified_email


@router.post("/{crew_id}/email-invites", status_code=status.HTTP_202_ACCEPTED)
def invite_by_email(crew_id: int, body: EmailInviteIn, db: DB, user: CurrentUser, tasks: BackgroundTasks):
    crew = _member_crew(db, crew_id, user)
    if len(crew.members) >= MAX_MEMBERS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This crew is full ({MAX_MEMBERS} members).")
    if not invite_limiter.hit(str(user.id), INVITES_PER_HOUR):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "That's a lot of invites. Try again in an hour.")
    email = str(body.email).strip().lower()
    member_emails = {m.user.email for m in crew.members}
    waiting = db.scalar(select(CrewEmailInvite).where(CrewEmailInvite.crew_id == crew.id, CrewEmailInvite.email == email))
    if waiting is not None and _aware(waiting.expires_at) <= datetime.now(UTC):
        db.delete(waiting)  # an old one that ran out: start again
        db.flush()
        waiting = None
    if email not in member_emails and waiting is None:  # already in, or already invited: say the same, change nothing
        now = datetime.now(UTC)
        # Invites made in the last day still have more than 13 of their 14 days left.
        today = db.scalar(select(func.count()).select_from(CrewEmailInvite).where(
            CrewEmailInvite.email == email,
            CrewEmailInvite.expires_at > now + timedelta(days=EMAIL_INVITE_DAYS - 1))) or 0
        db.add(CrewEmailInvite(crew_id=crew.id, email=email, invited_by=user.id,
                               expires_at=now + timedelta(days=EMAIL_INVITE_DAYS)))
        db.commit()
        confirmed = user.email_verified or not get_settings().require_verified_email  # unconfirmed inviters email nobody
        if confirmed and today < INVITE_EMAILS_PER_ADDRESS_PER_DAY:
            tasks.add_task(mail.send, mail.crew_invite(email, short_name(user.display_name), crew.name, EMAIL_INVITE_DAYS))
    return {"message": SENT}


@router.delete("/{crew_id}/email-invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_email_invite(crew_id: int, invite_id: int, db: DB, user: CurrentUser):
    """Whoever sent the invite, or the owner, can cancel it."""
    crew = _member_crew(db, crew_id, user)
    invite = db.get(CrewEmailInvite, invite_id)
    if invite is None or invite.crew_id != crew.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    if invite.invited_by != user.id and crew.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only whoever sent this invite, or the crew leader, can cancel it")
    db.delete(invite)
    db.commit()


def _pending(db: DB, user: User) -> list[CrewEmailInvite]:
    if not _sees_invites(user):
        return []
    rows = db.scalars(select(CrewEmailInvite).where(CrewEmailInvite.email == user.email).order_by(CrewEmailInvite.created_at)).all()
    now = datetime.now(UTC)
    return [i for i in rows if _aware(i.expires_at) > now and not any(m.user_id == user.id for m in i.crew.members)]


@router.get("/pending", response_model=list[PendingInviteOut])
def pending_invites(db: DB, user: CurrentUser):
    """Invites waiting for you, oldest first."""
    return [PendingInviteOut(id=i.id, crew_id=i.crew_id, crew_name=i.crew.name, description=i.crew.description,
                             invited_by=name_of(i.inviter), members=len(i.crew.members),
                             full=len(i.crew.members) >= MAX_MEMBERS, expires_at=i.expires_at) for i in _pending(db, user)]


def _my_invite(db: DB, user: User, invite_id: int) -> CrewEmailInvite:
    invite = next((i for i in _pending(db, user) if i.id == invite_id), None)
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This invite has expired or was cancelled.")
    return invite


@router.post("/pending/{invite_id}/accept", response_model=CrewOut)
def accept_invite(invite_id: int, db: DB, user: CurrentUser):
    invite = _my_invite(db, user, invite_id)
    crew = invite.crew
    if len(crew.members) >= MAX_MEMBERS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This crew is full ({MAX_MEMBERS} members).")
    crew.members.append(CrewMember(user_id=user.id))
    db.delete(invite)
    db.commit()
    db.refresh(crew)
    return _out(db, crew, user)


@router.post("/pending/{invite_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
def decline_invite(invite_id: int, db: DB, user: CurrentUser):
    """Say no. The inviter isn't told; the invite just goes."""
    db.delete(_my_invite(db, user, invite_id))
    db.commit()

