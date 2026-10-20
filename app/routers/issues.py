"""Report an issue (CC-84): signed-in members report problems with the site; admins review them on the
Issues page, set a status (new, looking into it, fixed, won't fix) and can add a note the member sees.
New reports appear in the Tuesday recap. No screenshots yet."""
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from ..deps import DB, AdminUser, CurrentUser
from ..models import IssueReport
from ..ratelimit import SlidingWindowLimiter
from ..schemas import IssueAdminOut, IssueIn, IssueOut, IssuePage, IssuePatch, IssueReporter, IssueStatus

router = APIRouter(tags=["issues"])
REPORTS_PER_HOUR = 5
report_limiter = SlidingWindowLimiter(window_seconds=3600)


@router.post("/issues", response_model=IssueOut, status_code=status.HTTP_201_CREATED)
def report(body: IssueIn, db: DB, user: CurrentUser):
    """Report a problem. Signed-in members only; 5 an hour."""
    if not report_limiter.hit(str(user.id), REPORTS_PER_HOUR):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Thanks for all the reports! Try again in an hour.")
    issue = IssueReport(user_id=user.id, **body.model_dump())
    db.add(issue)
    db.commit()
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


@router.get("/admin/issues", response_model=IssuePage)
def all_reports(db: DB, _: AdminUser, show: Annotated[IssueStatus | None, Query(description="Only this status")] = None):
    """Every report, newest first (or one status), with who sent it and the count per status."""
    q = select(IssueReport).order_by(IssueReport.created_at.desc(), IssueReport.id.desc())
    if show:
        q = q.where(IssueReport.status == show)
    counts = dict(db.execute(select(IssueReport.status, func.count()).group_by(IssueReport.status)).all())
    return IssuePage(items=[_admin_out(i) for i in db.scalars(q)], counts=counts)


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
