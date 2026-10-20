import json
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

from ..deps import DB
from ..stats import today_in_chicago
from ..traffic import VIEWS, is_bot, record, visitor_key

router = APIRouter(prefix="/traffic", tags=["traffic"])


class Ping(BaseModel):
    kind: Literal["view", "ticket", "outing"] = "view"
    view: str = Field(default="", max_length=20)
    vid: str | None = Field(default=None, min_length=16, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


@router.post("", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def ping(request: Request, db: DB):
    """CC-68: count one view, ticket click or planned outing. Anyone may send it; it never fails the page.
    The site sends it as text/plain JSON so the browser needs no preflight request."""
    body = (await request.body())[:500]
    try:
        p = Ping.model_validate(json.loads(body or b"{}"))
    except (ValueError, ValidationError):
        return Response(status_code=status.HTTP_204_NO_CONTENT)  # ignore junk quietly
    agent = request.headers.get("user-agent", "")
    if is_bot(agent) or (p.kind == "view" and p.view not in VIEWS):
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    address = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()  # set by Caddy, the only way in
               or (request.client.host if request.client else ""))
    day = today_in_chicago()
    visitor, lasting = visitor_key(p.vid, day, address, agent)

    def save():  # the database calls are blocking: keep them off the event loop
        record(db, day, p.kind, p.view, visitor, lasting)
        db.commit()
    await run_in_threadpool(save)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
