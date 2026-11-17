"""CC-112: takes. A member can share a Cultural Journal entry (its three words, review and whether they'd
recommend it) on the event's public page (/e/...) and in the site's event view. Off by default, chosen per entry;
unsharing or un-marking "I've been" takes it down at once (the /e/ page within its cache time). An admin can hide
a take without touching the entry. The member isn't told, and it stays hidden until an admin unhides it.
"""
from sqlalchemy import or_, select

from .crewplans import my_crew_ids
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
    """The shared takes this viewer may see on an event, newest first (CC-172). The admin's own takes ("Yours truly")
    show to everyone, signed out too. Another member's take shows only to the members of a crew they're in, never to
    the public. Reviews go only to admins (who moderate): everyone else gets the three words, the name and whether
    they'd recommend it. Admins see every take, the hidden ones marked, with whose they are (so they can hide or
    unhide them); nobody else ever sees a hidden take or a member's id."""
    admins_view = viewer is not None and viewer.role == Role.admin
    q = (select(Attended, User).join(User, User.id == Attended.user_id)
         .where(Attended.event_id == event_id, Attended.shared_at.is_not(None), Attended.words != "")
         .order_by(Attended.shared_at.desc()))
    if not admins_view:
        q = q.where(Attended.hidden.is_(False))
        mates = set()
        if viewer is not None:
            mine = my_crew_ids(db, viewer)
            if mine:
                mates = set(db.scalars(select(CrewMember.user_id).where(CrewMember.crew_id.in_(mine)))) - {viewer.id}
        q = q.where(or_(User.role == Role.admin, User.id.in_(mates)))
    return [{"words": a.words, "review": a.review if admins_view else "", "recommend": a.recommend, "shared_at": a.shared_at,
             "name": YOURS_TRULY if u.role == Role.admin else ANONYMOUS if a.anonymous else public_name(u.display_name),
             "by_admin": u.role == Role.admin,
             **({"user_id": u.id, "hidden": a.hidden} if admins_view else {})}
            for a, u in db.execute(q)]


def attach_takes(db, event: Event, user: User | None) -> Event:
    event.takes = takes_for(db, event.id, user)
    return event
