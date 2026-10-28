"""CC-112: public takes. A member can share a Cultural Journal entry (its three words, review and whether they'd
recommend it) on the event's public page (/e/...) and in the site's event view. Off by default, chosen per entry;
unsharing or un-marking "I've been" takes it down at once (the /e/ page within its cache time). An admin can hide
a take without touching the entry. The member isn't told, and it stays hidden until an admin unhides it.
"""
from sqlalchemy import select

from .models import Attended, Event, Role, User

ANONYMOUS = "A Culture Crew member"


def public_name(display_name: str) -> str:
    """"Jordan Reyes" -> "Jordan R.", as the site shortens names for crews; one word stays as it is."""
    parts = display_name.split()
    if not parts:
        return ANONYMOUS
    if len(parts) > 1 and parts[-1][:1].isalpha():
        return f"{parts[0]} {parts[-1][0].upper()}."
    return " ".join(parts)


def takes_for(db, event_id: int, admins_view: bool = False) -> list[dict]:
    """The shared takes on an event, newest first. Admins also get the hidden ones, marked, with whose they are
    (so they can hide or unhide them); nobody else ever sees a hidden take or a member's id."""
    q = (select(Attended, User).join(User, User.id == Attended.user_id)
         .where(Attended.event_id == event_id, Attended.shared_at.is_not(None), Attended.words != "")
         .order_by(Attended.shared_at.desc()))
    if not admins_view:
        q = q.where(Attended.hidden.is_(False))
    return [{"words": a.words, "review": a.review, "recommend": a.recommend, "shared_at": a.shared_at,
             "name": ANONYMOUS if a.anonymous else public_name(u.display_name),
             **({"user_id": u.id, "hidden": a.hidden} if admins_view else {})}
            for a, u in db.execute(q)]


def attach_takes(db, event: Event, user: User | None) -> Event:
    event.takes = takes_for(db, event.id, admins_view=user is not None and user.role == Role.admin)
    return event
