"""Report an issue (CC-84): signed-in members report problems with the site; admins review them on the
Issues page, set a status (new, looking into it, fixed, won't fix) and can add a note the member sees.
New reports appear in the Tuesday recap. No screenshots yet.
CC-95: a report can be a feature request instead. Issues and requests are numbered apart (ISS-0007, FR-0002)."""
import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from ..deps import DB, AdminUser, CurrentUser
from ..models import IssueReport, User
from ..ratelimit import SlidingWindowLimiter
from ..schemas import IssueAdminOut, IssueIn, IssueKind, IssueOut, IssuePage, IssuePatch, IssueReporter, IssueStatus

router = APIRouter(tags=["issues"])
REPORTS_PER_HOUR = 5
report_limiter = SlidingWindowLimiter(window_seconds=3600)


@router.post("/issues", response_model=IssueOut, status_code=status.HTTP_201_CREATED)
def report(body: IssueIn, db: DB, user: CurrentUser):
    """Report a problem, or request a feature. Signed-in members only; 5 an hour, both kinds together."""
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
