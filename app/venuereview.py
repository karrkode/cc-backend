"""CC-188: a venue a member's submission adds waits for an admin. It stays out of the public venue list until one of its
events is approved. A rejected event keeps its venue (it stays on the member's list and can be approved after all), so
the venue goes only once no event is left at it, as agreed on the story."""
from sqlalchemy import select

from .models import DiscoverySource, Event, SubmissionRule, Venue


def accept_venue(venue: Venue, category: str, opera: bool) -> None:
    """An approved event: its venue is live. A venue with no default yet takes this event's."""
    venue.pending = False
    if venue.category is None:
        venue.category, venue.opera = category, opera


def drop_if_unused(db, venue_id: int) -> None:
    """An event left this venue: remove it if it's still pending and nothing else uses it."""
    db.flush()
    venue = db.get(Venue, venue_id)
    if venue is None or not venue.pending:
        return
    users = (select(Event.id).where(Event.venue_id == venue_id), select(DiscoverySource.id).where(DiscoverySource.venue_id == venue_id),
             select(SubmissionRule.id).where(SubmissionRule.venue_id == venue_id))
    if not any(db.scalar(q.limit(1)) for q in users):
        db.delete(venue)
