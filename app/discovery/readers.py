"""Turning a source's pages into found events.

- aic_api: the Art Institute of Chicago's own public API (exhibitions and events), no AI needed.
- claude: Claude Haiku 5.5 reads a page's text and returns events in a fixed, validated shape
  (structured output). It is told the product owner's rules; rules.py checks them again.
"""
import json
import urllib.request
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from .fetch import USER_AGENT, TIMEOUT

MODEL = "claude-haiku-5-5"
# Claude Haiku 5.5 list price per million tokens (prompts up to 100K tokens), for estimates only; the
# Anthropic Console has the exact bill.
PRICE_IN, PRICE_OUT = 0.10, 0.50


def cost_usd(tokens_in: int, tokens_out: int) -> float:
    return round(tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT, 4)
Kind = Literal["production", "exhibition", "concert", "comedy_show", "talk", "tour", "program", "class",
               "rental", "members_only", "recurring", "permanent", "other"]


class Performance(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    time: str | None = Field(default=None, description="24-hour HH:MM, if given")


class ShowDay(BaseModel):
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    times: list[str] = Field(description="24-hour HH:MM start times on that day")
    weeks: list[int] = Field(default_factory=list, description="Only if it's on some weeks of the month: 1-4 for the "
                             "first to fourth, 5 for the last. Empty means every week")


class FoundEvent(BaseModel):
    title: str
    url: str | None = Field(default=None, description="The event's own page, if the text links to it")
    kind: Kind
    category: Literal["Theater", "Comedy", "Music", "Visual Art", "Educational"] | None = None  # CC-125
    venue_name: str | None = Field(default=None, description="Only if different from the source's usual venue")
    begin_date: str | None = Field(default=None, description="YYYY-MM-DD: the first day, if known")
    end_date: str | None = Field(default=None, description="YYYY-MM-DD: the last day, if known")
    open_run: bool = Field(default=False, description="Runs with no announced closing date")
    cost_text: str | None = Field(default=None, description="Prices as written, e.g. '$35–$90' or 'Free'")
    min_price: float | None = Field(default=None, description="The lowest price in dollars; 0 if free")
    performances: list[Performance] = Field(default_factory=list, description="Specific dates and times, if listed")
    show_days: list[ShowDay] = Field(default_factory=list, description="The regular weekly schedule, if the page gives one")
    recurring: bool = Field(default=False, description="Repeats daily or weekly as a generic offering")


class OpeningHours(BaseModel):
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    open: str | None = Field(default=None, description="HH:MM, or null if closed that day")
    close: str | None = None


class PageReading(BaseModel):
    events: list[FoundEvent]
    venue_hours: list[OpeningHours] = Field(default_factory=list, description="Only if the page states regular opening hours")


INSTRUCTIONS = """You read a Chicago cultural venue's own web page and list the public events on it, for a
curated listings site. Today is {today}. The source is {source} ({kind}). The page address is {url}.

List each event once:
- A theater or opera production is one event covering its whole run, with its first and last dates.
  If the page lists individual performance dates and times, put them in `performances`.
- A concert or a comedy show is one event per show or night. A show that repeats under the same title
  (a weekly comedy show, an open mic, a jazz night) is one event; with no end date it has open_run true.
  List the dates shown in `performances`.
- When the page gives a regular weekly schedule ("Thursdays to Saturdays at 8 PM, Sundays at 3 PM",
  "every Monday at 7:30"), put it in `show_days`: each weekday with its start times. A day that's only on
  some weeks of the month ("first and third Thursdays", "the last Sunday of the month") gives those weeks.
- A museum exhibition is one event with its opening and closing dates. One-off talks, tours and
  programs are events too (kind talk, tour or program).
Mark, rather than leave out, these kinds so they can be filtered: classes and workshops (kind class),
private rentals (rental), members-only events (members_only), a museum's permanent galleries and
long-term displays with no closing date (permanent), and generic offerings that repeat daily
or weekly without a specific experience, such as family days or daily highlights tours (recurring, and
recurring true). A repeating performance (a weekly comedy show, an open mic) isn't one of these.
Leave out past events, gift cards, memberships, subscriptions, donation drives and anything that isn't
an event. Use only what the page says: never guess dates or prices; leave a field empty when unknown.
Use full YYYY-MM-DD dates, working out the year from today's date when the page leaves it out.
Give each event's own page address when the text links to it (links appear in <angle brackets>).
If the page states the venue's regular opening hours, give them in venue_hours."""


def read_page(client, source_name: str, kind: str, url: str, text: str, today: date) -> tuple[PageReading, int, int]:
    """(reading, input_tokens, output_tokens). Raises on a refusal or an unusable reply."""
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "low"},
        system=INSTRUCTIONS.format(today=today.isoformat(), source=source_name, kind=kind, url=url),
        messages=[{"role": "user", "content": "The page's text:\n\n" + text}],
        output_format=PageReading,
    )
    if response.stop_reason == "refusal":
        raise ValueError("the model declined to read this page")
    if response.stop_reason == "max_tokens":
        raise ValueError("the reply was cut off (the page lists too much at once)")
    usage = response.usage
    return response.parsed_output, usage.input_tokens, usage.output_tokens


# ---- the Art Institute of Chicago's public API (no AI) ----------------------------------

AIC = "https://api.artic.edu/api/v1"
AIC_HEADERS = {"User-Agent": USER_AGENT, "AIC-User-Agent": USER_AGENT, "Content-Type": "application/json"}


def _aic(path: str, body: dict) -> dict:
    req = urllib.request.Request(AIC + path, data=json.dumps(body).encode(), headers=AIC_HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 - fixed https address
        return json.loads(r.read())


def read_aic(today: date) -> PageReading:
    """Exhibitions still open or upcoming, and upcoming events, flagged the way the rules expect."""
    ex = _aic("/exhibitions/search", {"query": {"range": {"aic_end_at": {"gte": "now"}}}, "limit": 100,
                                       "fields": ["id", "title", "aic_start_at", "aic_end_at", "web_url", "status"]})
    events = [FoundEvent(title=x["title"], url=x.get("web_url"), kind="exhibition", category="Visual Art",
                         begin_date=(x.get("aic_start_at") or "")[:10] or None, end_date=(x.get("aic_end_at") or "")[:10] or None)
              for x in ex.get("data", []) if x.get("status") != "Closed"]
    ev = _aic("/events/search", {"query": {"range": {"start_date": {"gte": today.isoformat()}}}, "limit": 100,
                                  "fields": ["id", "title", "start_date", "end_date", "start_time", "web_url", "slug",
                                             "is_member_exclusive", "is_private", "is_virtual_event", "is_free",
                                             "program_titles", "event_type_id"]})
    for x in ev.get("data", []):
        kind = "members_only" if x.get("is_member_exclusive") else "rental" if x.get("is_private") else "program"
        if x.get("is_virtual_event"):
            continue  # online only: not an outing
        day = (x.get("start_date") or "")[:10] or None
        time = (x.get("start_time") or "")[:5] or None
        url = x.get("web_url") or (f"https://www.artic.edu/events/{x['id']}/{x.get('slug', '')}" if x.get("id") else None)
        events.append(FoundEvent(title=x["title"], url=url, kind=kind, category="Visual Art", begin_date=day, end_date=day,
                                 cost_text="Free" if x.get("is_free") else None, min_price=0 if x.get("is_free") else None,
                                 performances=[Performance(date=day, time=time)] if day else []))
    return PageReading(events=events)
