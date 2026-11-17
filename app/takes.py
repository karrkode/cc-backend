"""CC-112, CC-172: takes. A member can share a Cultural Journal entry (its three words and whether they'd recommend
it, never the review) on the event's page. Off by default, chosen per entry; unsharing or un-marking "I've been" takes it
down at once (the /e/ page within its cache time). Who sees it: everyone sees Yours truly's (the dolphins account), and
the members of the author's crews see a member's; the public /e/ page shows only Yours truly's. An admin can hide a take
without touching the entry. The member isn't told, and it stays hidden until an admin unhides it.
"""
from sqlalchemy import or_, select

from .models import Attended, CrewMember, Event, Role, User

ANONYMOUS = "A Culture Crew member"
YOURS_TRULY = "Yours truly"  # CC-172: the admin signs as this on the site, whatever the account is called


def public_name(display_name: str) -> str:
    """"Jordan Reyes" -> "Jordan R.", as the site shortens names for crews; one word stays as it is."""
    parts = display_name.split()
    if not parts:
        return ANONYMOUS
    if len(parts) > 1 and parts[-1][:1].isalpha():
        return f"{parts[0]} {parts[-1][0].upper()}."
    return " ".join(parts)


def takes_for(db, event_id: int, viewer: User | None = None) -> list[dict]:
    """The shared takes this viewer may see on an event, newest first (CC-172). "Yours truly" is the account with the
    dolphins (the same one that makes the picks): its takes show to everyone, signed out too, always under that name.
    Another member's take shows only to the members of a crew they're in, never to the public, and never to its own
    author (it's in their journal). Nobody gets a review: the three words, the name and whether they'd recommend it.
    Admins see every take, the hidden ones marked, with whose they are (so they can hide or unhide them); nobody else
    ever sees a hidden take or a member's id."""
    admins_view = viewer is not None and viewer.role == Role.admin
    q = (select(Attended, User).join(User, User.id == Attended.user_id)
         .where(Attended.event_id == event_id, Attended.shared_at.is_not(None), Attended.words != "")
         .order_by(Attended.shared_at.desc()))
    if not admins_view:
        q = q.where(Attended.hidden.is_(False))
        visible = User.dolphins.is_(True)
        if viewer is not None:
            mine = select(CrewMember.crew_id).where(CrewMember.user_id == viewer.id)
            mates = select(CrewMember.user_id).where(CrewMember.crew_id.in_(mine), CrewMember.user_id != viewer.id)
            visible = or_(visible, User.id.in_(mates))
        q = q.where(visible)
    return [{"words": a.words, "review": "", "recommend": a.recommend, "shared_at": a.shared_at,  # review: kept so the live site's shape holds
             "name": YOURS_TRULY if u.dolphins else ANONYMOUS if a.anonymous else public_name(u.display_name),
             "by_admin": u.dolphins,
             **({"user_id": u.id, "hidden": a.hidden} if admins_view else {})}
            for a, u in db.execute(q)]


def attach_takes(db, event: Event, user: User | None) -> Event:
    event.takes = takes_for(db, event.id, user)
    return event
