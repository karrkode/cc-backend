"""Auto-reject rules for members' submissions (CC-100). A submission matching a rule is saved as rejected,
with a reason naming the rule, and never reaches the review queue. The member sees the usual thanks.
Admins' own submissions are never checked. Separate from the discovery skip rules (CC-71), as agreed."""
from sqlalchemy import select

from .discovery.rules import norm_title
from .models import Event, EventStatus, SubmissionRule


def matching_rule(db, event: Event) -> SubmissionRule | None:
    words = f" {norm_title(event.title)} "
    return next((r for r in db.scalars(select(SubmissionRule).order_by(SubmissionRule.id))
                 if (r.kind == "venue" and r.venue_id == event.venue_id) or
                 (r.kind == "name" and r.phrase and f" {r.phrase} " in words)), None)


def rule_text(rule: SubmissionRule) -> str:
    if rule.kind == "venue":
        return f"venue “{rule.venue.name}”" if rule.venue else "a venue"
    return f"event name “{rule.phrase}”"


def reject_if_caught(db, event: Event) -> None:
    rule = matching_rule(db, event)
    if rule is not None:
        event.status = EventStatus.rejected
        event.reject_reason = f"Auto-reject rule: {rule_text(rule)}"[:200]
        rule.caught += 1
