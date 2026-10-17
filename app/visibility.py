"""What the public can see: approved events whose publish time (if any) has arrived.

Scheduled publishing needs no background job: an approved event with a future
publish_at is simply filtered out until then.
"""
from datetime import UTC, datetime

from sqlalchemy import and_, or_

from .models import Event, EventStatus, Role


def utcnow() -> datetime:
    return datetime.now(UTC)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite hands back naive datetimes


def live_condition(now: datetime | None = None):
    """SQL condition for events the public can see."""
    now = now or utcnow()
    return and_(Event.status == EventStatus.approved, or_(Event.publish_at.is_(None), Event.publish_at <= now))


def is_live(event: Event, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return event.status == EventStatus.approved and (event.publish_at is None or _utc(event.publish_at) <= now)


def is_scheduled(event: Event, now: datetime | None = None) -> bool:
    return event.status == EventStatus.approved and not is_live(event, now)


def visible_to(event: Event, user) -> bool:
    """Live events for everyone; anything else only for admins and the person who submitted it."""
    if is_live(event):
        return True
    return user is not None and (user.role == Role.admin or event.submitted_by == user.id)
