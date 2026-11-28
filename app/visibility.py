"""What the public can see: approved events whose publish time (if any) has arrived.

Scheduled publishing needs no background job: an approved event with a future
publish_at is simply filtered out until then.
"""
from datetime import datetime

from sqlalchemy import and_, or_

from .clock import utcnow, as_utc
from .models import Event, EventStatus


def live_condition(now: datetime | None = None):
    """SQL condition for events the public can see."""
    now = now or utcnow()
    return and_(Event.status == EventStatus.approved, or_(Event.publish_at.is_(None), Event.publish_at <= now))


def is_live(event: Event, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return event.status == EventStatus.approved and (event.publish_at is None or as_utc(event.publish_at) <= now)


def visible_to(event: Event, user) -> bool:
    """Live events for everyone; anything else only for admins and the person who submitted it."""
    if is_live(event):
        return True
    return user is not None and (user.is_admin or event.submitted_by == user.id)
