"""Culture crews (CC-74): people who go out together. A member can be in several crews; any member can
make an invite link; a crew has up to 25 members. Members see each other's names, never emails, and
nobody outside a crew can see who's in it (a crew you're not in is "not found").

Invite links carry a random token in the URL fragment (#join=…), which browsers never send to a server.
Only its hash is stored; a link lasts 7 days and can be revoked by whoever made it, or by the owner.
"""
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select

from ..config import get_settings
from ..deps import DB, CurrentUser, OptionalUser
from ..models import Crew, CrewInvite, CrewMember, User
from ..ratelimit import SlidingWindowLimiter
from ..schemas import (CrewIn, CrewInviteLink, CrewInviteOut, CrewInvitePreview, CrewMemberOut, CrewOut, CrewPatch,
                       JoinIn, OwnerIn)
from ..security import hash_refresh_token as hash_token

router = APIRouter(prefix="/crews", tags=["crews"])

MAX_MEMBERS = 25
MAX_OWNED = 10  # crews one person can start
INVITE_DAYS = 7
INVITES_PER_HOUR = 20
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
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the crew's owner can do that")
    return crew


def _out(db: DB, crew: Crew, user: User) -> CrewOut:
    invites = db.scalars(select(CrewInvite).where(CrewInvite.crew_id == crew.id).order_by(CrewInvite.created_at.desc())).all()
    return CrewOut(
        id=crew.id, name=crew.name, description=crew.description, owner=crew.owner_id == user.id, max_members=MAX_MEMBERS,
        members=[CrewMemberOut(user_id=m.user_id, name=name_of(m.user), owner=m.user_id == crew.owner_id, you=m.user_id == user.id,
                               joined_at=m.joined_at)
                 for m in sorted(crew.members, key=lambda m: (m.user_id != crew.owner_id, _aware(m.joined_at), name_of(m.user)))],
        invites=[CrewInviteOut(id=i.id, made_by=name_of(i.creator), yours=i.created_by == user.id, created_at=i.created_at,
                               expires_at=i.expires_at, uses=i.uses) for i in invites if _live(i)])


@router.get("", response_model=list[CrewOut])
def my_crews(db: DB, user: CurrentUser):
    """Your crews, oldest first, with their members and live invite links."""
    crews = db.scalars(select(Crew).join(CrewMember).where(CrewMember.user_id == user.id).order_by(Crew.created_at, Crew.id)).all()
    return [_out(db, c, user) for c in crews]


@router.post("", response_model=CrewOut, status_code=status.HTTP_201_CREATED)
def start_crew(body: CrewIn, db: DB, user: CurrentUser):
    owned = db.scalar(select(func.count()).select_from(Crew).where(Crew.owner_id == user.id))
    if owned >= MAX_OWNED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"You can start up to {MAX_OWNED} crews. Hand one over or delete one first.")
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
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only whoever made this link, or the owner, can revoke it")
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
        raise HTTPException(status.HTTP_409_CONFLICT, "You own this crew. Hand it to another member first, or delete it.")
    crew.members = [m for m in crew.members if m.user_id != user.id]
    db.commit()


@router.delete("/{crew_id}/members/{user_id}", response_model=CrewOut)
def remove_member(crew_id: int, user_id: uuid.UUID, db: DB, user: CurrentUser):
    crew = _owned_crew(db, crew_id, user)
    if user_id == user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You can't remove yourself. Hand the crew over, or delete it.")
    if not any(m.user_id == user_id for m in crew.members):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a member of this crew")
    crew.members = [m for m in crew.members if m.user_id != user_id]
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
