"""Report an issue (CC-84): signed-in members report problems with the site; admins review them on the
Issues page, set a status (new, looking into it, fixed, won't fix) and can add a note the member sees.
New reports appear in the Tuesday recap. No screenshots yet.
CC-95: a report can be a feature request instead. Issues and requests are numbered apart (ISS-0007, FR-0002).
CC-241: an admin can turn a report into a Jira ticket (one per report) and see that ticket's status in Jira. The
report's own status stays the admin's: a ticket that's done in Jira (deployed) doesn't mark the report fixed."""
import logging
import re
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .. import jira
from ..config import get_settings
from ..deps import DB, AdminUser, CurrentUser, require_verified_email
from ..models import IssueReport, User
from ..ratelimit import SlidingWindowLimiter
from ..schemas import (IssueAdminOut, IssueIn, IssueKind, IssueOut, IssuePage, IssuePatch, IssueReporter, IssueStatus, JiraCreate,
                       JiraDraft, JiraTicketStatus)

log = logging.getLogger("uvicorn.error")

router = APIRouter(tags=["issues"])
REPORTS_PER_HOUR = 5
report_limiter = SlidingWindowLimiter(window_seconds=3600)


@router.post("/issues", response_model=IssueOut, status_code=status.HTTP_201_CREATED)
def report(body: IssueIn, db: DB, user: CurrentUser):
    """Report a problem, or request a feature. Signed-in members only; 5 an hour, both kinds together."""
    require_verified_email(user, "sending reports")  # CC-186
    if not report_limiter.hit(str(user.id), REPORTS_PER_HOUR):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Thanks for all the reports! Try again in an hour.")
    for attempt in range(3):  # two reports at once could take the same number: the second tries the next one
        number = (db.scalar(select(func.max(IssueReport.number)).where(IssueReport.kind == body.kind)) or 0) + 1
        issue = IssueReport(user_id=user.id, number=number, **body.model_dump())
        db.add(issue)
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise
    db.refresh(issue)
    return issue


@router.get("/me/issues", response_model=list[IssueOut])
def my_reports(db: DB, user: CurrentUser):
    """Your reports, newest first, with their status and any note from us."""
    return db.scalars(select(IssueReport).where(IssueReport.user_id == user.id)
                      .order_by(IssueReport.created_at.desc(), IssueReport.id.desc())).all()


def _admin_out(issue: IssueReport) -> IssueAdminOut:
    out = IssueAdminOut.model_validate(issue)
    if issue.reporter is not None:
        out.sent_by = IssueReporter(name=issue.reporter.display_name.strip() or "No name given", email=issue.reporter.email)
    if issue.jira_key:
        out.jira_url = jira.ticket_url(issue.jira_key)
    return out


TICKET = re.compile(r"^(?:(iss|fr)[-\s]?)?#?0*(\d{1,9})$", re.I)  # ISS-0007, iss-7, FR-2, #7, 7 (either kind)


@router.get("/admin/issues", response_model=IssuePage)
def all_reports(db: DB, _: AdminUser, show: Annotated[IssueStatus | None, Query(description="Only this status")] = None,
                kind: Annotated[IssueKind | None, Query(description="Only issues, or only feature requests (CC-95)")] = None,
                q: Annotated[str | None, Query(max_length=100, description="A ticket number (ISS-0007, FR-2, or 7), or words to find")] = None):
    """Every report, newest first (or one status, or one kind), with who sent it and the count per status. q finds a
    ticket by its number, or searches what was reported and the sender's name and email."""
    stmt = select(IssueReport).outerjoin(User, User.id == IssueReport.user_id).order_by(IssueReport.created_at.desc(), IssueReport.id.desc())
    if show:
        stmt = stmt.where(IssueReport.status == show)
    if kind:
        stmt = stmt.where(IssueReport.kind == kind)
    term = (q or "").strip()
    if term:
        ticket = TICKET.match(term)
        if ticket:
            stmt = stmt.where(IssueReport.number == int(ticket.group(2)))
            if ticket.group(1):
                stmt = stmt.where(IssueReport.kind == ("feature" if ticket.group(1).lower() == "fr" else "issue"))
        else:
            stmt = stmt.where(or_(IssueReport.what_happened.icontains(term, autoescape=True), IssueReport.expected.icontains(term, autoescape=True),
                                  IssueReport.page.icontains(term, autoescape=True), User.display_name.icontains(term, autoescape=True),
                                  User.email.icontains(term, autoescape=True)))
    q = stmt
    counted = select(IssueReport.status, func.count()).group_by(IssueReport.status)
    counts = dict(db.execute(counted.where(IssueReport.kind == kind) if kind else counted).all())
    kinds = dict(db.execute(select(IssueReport.kind, func.count()).where(IssueReport.status.in_(["new", "looking"])).group_by(IssueReport.kind)).all())
    return IssuePage(items=[_admin_out(i) for i in db.scalars(q).unique()], counts=counts, kinds=kinds)


@router.patch("/admin/issues/{issue_id}", response_model=IssueAdminOut)
def update_report(issue_id: int, body: IssuePatch, db: DB, _: AdminUser):
    """Set the status, and/or the note the member sees."""
    issue = db.get(IssueReport, issue_id)
    if issue is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    if body.status is not None:
        issue.status = body.status
    if body.admin_note is not None:
        issue.admin_note = body.admin_note.strip()
    db.commit()
    db.refresh(issue)
    return _admin_out(issue)


# ---- CC-241: Jira tickets ---------------------------------------------------------------

CLAIM = timedelta(minutes=1)  # how long a ticket being made holds the report; then a retry may go ahead
KEEP = " Your text is still here."


def _report(db, issue_id: int) -> IssueReport:
    issue = db.get(IssueReport, issue_id)
    if issue is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found")
    return issue


def _reference(issue: IssueReport) -> str:
    return (f"From Culture Crew report {issue.key}, sent {issue.created_at:%Y-%m-%d}. "
            f"Admins can open it at {get_settings().site_url.rstrip('/')}/#issues/{issue.key}")


@router.get("/admin/issues/{issue_id}/jira", response_model=JiraDraft)
def ticket_draft(issue_id: int, db: DB, _: AdminUser):
    """The ticket the server suggests for this report: a Bug for an issue, a Story for a feature request, Medium
    priority, with emails, phone numbers, tokens, URL query strings and the reporter's name taken out."""
    issue = _report(db, issue_id)
    name = issue.reporter.display_name if issue.reporter else ""
    feature = issue.kind == "feature"
    first = jira.redact(issue.what_happened, name).strip().split("\n")[0].strip()
    summary = f"{issue.key}: {first}"
    if len(summary) > 120:
        summary = summary[:119].rstrip() + "…"
    parts = [("The idea" if feature else "What happened", issue.what_happened),
             ("Why it would help" if feature else "What they expected", issue.expected)]
    text = [f"{head}\n{jira.redact(body, name).strip()}" for head, body in parts if body.strip()]
    details = [f"{word}: {jira.redact(value, name)}" for word, value in
               (("Page", issue.page), ("Browser", issue.browser), ("Screen", issue.screen), ("Site version", issue.site_version)) if value]
    if details:
        text.append("\n".join(details))
    return JiraDraft(issue_type="Story" if feature else "Bug", priority="Medium", summary=summary, description="\n\n".join(text),
                     reference=_reference(issue), connected=get_settings().jira_mode != "none")


def _set(db, issue_id: int, *where, **values) -> int:
    """Change the report's Jira columns only; updated_at stays as it was, as it's when the member's report last changed."""
    done = db.execute(update(IssueReport).where(IssueReport.id == issue_id, *where)
                      .values(updated_at=IssueReport.updated_at, **values)
                      .execution_options(synchronize_session=False)).rowcount  # the caller refreshes what it reads
    db.commit()
    return done


@router.post("/admin/issues/{issue_id}/jira", response_model=IssueAdminOut)
def make_ticket(issue_id: int, body: JiraCreate, db: DB, _: AdminUser):
    """Make the Jira ticket for this report, once. A repeat click, a retry or a second admin gets the ticket that's
    already linked; while one is being made, others get a 409. Before making one, Jira is searched for the report's
    label, so a ticket that a timed-out try made is linked rather than made again."""
    issue = _report(db, issue_id)
    if issue.jira_key:
        return _admin_out(issue)
    now = datetime.now(UTC)
    if not _set(db, issue_id, IssueReport.jira_key.is_(None),
                or_(IssueReport.jira_pending_at.is_(None), IssueReport.jira_pending_at < now - CLAIM), jira_pending_at=now):
        db.refresh(issue)
        if issue.jira_key:
            return _admin_out(issue)
        raise HTTPException(status.HTTP_409_CONFLICT, "A ticket for this report is being made. Wait a minute, then look again.")
    name = issue.reporter.display_name if issue.reporter else ""
    label, started = jira.report_label(issue.key), monotonic()
    try:
        j = jira.client()
        found = j.find([label])
        key = found[0].key if found else j.create(
            body.issue_type, body.priority, jira.redact(body.summary, name),
            jira.redact(body.description, name) + "\n\n" + _reference(issue), [jira.FEEDBACK_LABEL, label])
    except jira.JiraError as e:
        log.warning("jira: ticket for %s failed: %s %s after %.1fs", issue.key, e.status, type(e.__cause__).__name__ if e.__cause__ else "",
                    monotonic() - started)
        if not e.maybe_made:
            _set(db, issue_id, jira_pending_at=None)
        detail = e.message + (" Wait a minute, then try again: if the ticket was made, it will be linked, not made twice."
                              if e.maybe_made else KEEP)
        raise HTTPException(e.status, detail, headers={"Retry-After": str(e.retry_after)} if e.retry_after else None) from None
    except Exception:
        _set(db, issue_id, jira_pending_at=None)
        log.exception("jira: ticket for %s failed", issue.key)
        raise
    _set(db, issue_id, jira_key=key, jira_created_at=datetime.now(UTC), jira_pending_at=None)
    log.info("jira: %s %s %s in %.1fs", issue.key, "linked to existing" if found else "made", key, monotonic() - started)
    db.refresh(issue)
    return _admin_out(issue)


@router.get("/admin/jira-status", response_model=dict[int, JiraTicketStatus])
def ticket_statuses(db: DB, _: AdminUser, ids: Annotated[list[int], Query(max_length=100, description="Report ids")]):
    """The Jira status of the tickets made from these reports, by report id. A report whose ticket can't be found
    (deleted in Jira) is left out."""
    linked = db.scalars(select(IssueReport).where(IssueReport.id.in_(ids), IssueReport.jira_key.is_not(None))).unique().all()
    if not linked:
        return {}
    by_label = {jira.report_label(i.key): i.id for i in linked}
    try:
        tickets = jira.client().find(list(by_label))
    except jira.JiraError as e:
        log.warning("jira: status lookup failed: %s", e.status)
        raise HTTPException(e.status, e.message, headers={"Retry-After": str(e.retry_after)} if e.retry_after else None) from None
    out: dict[int, JiraTicketStatus] = {}
    for t in tickets:
        for lab in t.labels:
            if lab in by_label:
                out[by_label[lab]] = JiraTicketStatus(key=t.key, url=jira.ticket_url(t.key), status=t.status, category=t.category)
    return out
