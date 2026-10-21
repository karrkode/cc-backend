"""Approvers and the photo approvals queue (CC-91), as agreed with the product owner.

- Approver is a permission a member holds. Only an admin can offer it; the member accepts or declines
  (after signing in, under "Waiting for you"). Admins can always approve; so info@ (the admin) can.
- Approvers see "I've been" photos waiting for approval, oldest first: the event, the member by first
  name and last initial, what Amazon Rekognition found, and borderline photos blurred until they choose
  to look. Approve, or reject with an optional reason the member sees. Nobody approves their own photo.
- A rejected photo is deleted from storage at once; its row stays so the member sees the reason.
"""
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select

from .. import photos
from ..deps import DB, AdminUser, ApproverUser, CurrentUser
from ..models import Event, Role, User, WentPhoto
from ..schemas import (ApprovalOut, ApprovalsPage, ApproverAnswerIn, DecisionIn, EventRef, MemberOut, UserOut)

router = APIRouter(tags=["approvals"])


def short_name(name: str) -> str:
    """"Jordan Rivera" -> "Jordan R."; one word stays as it is."""
    parts = (name or "").split()
    if not parts:
        return "A member"
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1][0].upper()}."


# ---- the admin offers the permission ----------------------------------------------------

def _member(db: DB, user_id) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    return user


def _member_out(u: User) -> MemberOut:
    return MemberOut(id=u.id, display_name=u.display_name, email=u.email, role=u.role, approver=u.approver,
                     approver_offered=u.approver_offered)


@router.get("/admin/members", response_model=list[MemberOut])
def members(db: DB, _: AdminUser, q: Annotated[str, Query(max_length=100)] = "",
            show: Literal["all", "approvers"] = "all", limit: Annotated[int, Query(ge=1, le=200)] = 50):
    """Members by name, to offer or remove the approver permission. Approvers and open offers first."""
    query = select(User)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.where(or_(func.lower(User.display_name).like(like), User.email.like(like)))
    if show == "approvers":
        query = query.where(or_(User.approver, User.approver_offered_at.is_not(None)))
    rows = db.scalars(query.order_by(User.approver.desc(), User.approver_offered_at.is_(None),
                                     func.lower(User.display_name), User.email).limit(limit)).all()
    return [_member_out(u) for u in rows]


@router.post("/admin/members/{user_id}/approver", response_model=MemberOut)
def offer_approver(user_id: uuid.UUID, db: DB, _: AdminUser):
    """Offer the approver permission. It applies once the member accepts."""
    user = _member(db, user_id)
    if user.role == Role.admin:
        raise HTTPException(status.HTTP_409_CONFLICT, "Admins can already approve photos")
    if user.approver:
        raise HTTPException(status.HTTP_409_CONFLICT, "They're already an approver")
    user.approver_offered_at = datetime.now(UTC)
    db.commit()
    return _member_out(user)


@router.delete("/admin/members/{user_id}/approver", response_model=MemberOut)
def remove_approver(user_id: uuid.UUID, db: DB, _: AdminUser):
    """Take the permission away, or withdraw an offer they haven't answered."""
    user = _member(db, user_id)
    user.approver, user.approver_offered_at = False, None
    db.commit()
    return _member_out(user)


@router.post("/me/approver", response_model=UserOut)
def answer_offer(body: ApproverAnswerIn, db: DB, user: CurrentUser):
    """Accept or decline the admin's offer."""
    if not user.approver_offered:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There's no offer waiting")
    if body.accept:
        user.approver = True
    else:
        user.approver_offered_at = None
    db.commit()
    return user


# ---- the approvals queue ----------------------------------------------------------------

def _approval_out(db: DB, p: WentPhoto) -> ApprovalOut:
    event, member = db.get(Event, p.event_id), db.get(User, p.user_id)
    return ApprovalOut(id=p.id, status=p.status, event=EventRef(id=event.id, title=event.title), venue=event.venue.name,
                       member=short_name(member.display_name if member else ""), labels=p.labels or [], blurred=p.blurred,
                       reason=p.reason or "", created_at=p.created_at, url=photos.link(p) if p.status != "rejected" else "")


@router.get("/approvals", response_model=ApprovalsPage)
def approvals(db: DB, user: ApproverUser, show: Literal["waiting", "approved", "rejected"] = "waiting",
              limit: Annotated[int, Query(ge=1, le=100)] = 50):
    """Photos to approve, oldest first (or those decided, newest first). Your own photos aren't listed."""
    order = WentPhoto.created_at.asc() if show == "waiting" else WentPhoto.reviewed_at.desc()
    rows = db.scalars(select(WentPhoto).where(WentPhoto.status == show, WentPhoto.user_id != user.id)
                      .order_by(order, WentPhoto.id).limit(limit)).all()
    counts = dict(db.execute(select(WentPhoto.status, func.count()).where(WentPhoto.user_id != user.id)
                             .group_by(WentPhoto.status)).all())
    return ApprovalsPage(items=[_approval_out(db, p) for p in rows], counts=counts)


@router.post("/approvals/{photo_id}", response_model=ApprovalOut)
def decide(photo_id: int, body: DecisionIn, db: DB, user: ApproverUser):
    """Approve the photo, or reject it (it's deleted from storage; the member sees the reason)."""
    photo = db.get(WentPhoto, photo_id)
    if photo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo not found")
    if photo.user_id == user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Someone else approves your own photos")
    if photo.status != "waiting":
        raise HTTPException(status.HTTP_409_CONFLICT, "Someone has already decided on this photo")
    photo.status = "approved" if body.decision == "approve" else "rejected"
    photo.reason = body.reason.strip() if body.decision == "reject" else ""
    photo.reviewed_by, photo.reviewed_at = user.id, datetime.now(UTC)
    if photo.status == "rejected":
        try:
            photos.store().delete(photo.key)
        except Exception:  # noqa: BLE001 - the row says rejected; the file is never shown again
            pass
    db.commit()
    return _approval_out(db, photo)
