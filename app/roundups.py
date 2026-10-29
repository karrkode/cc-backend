"""CC-109: roundup pages at /chicago/<slug>, for people who search by time, price, kind or side of town
("this weekend in Chicago", "free things to do in Chicago"). Plain HTML like the event pages (app/seo.py),
served live through CloudFront, so they stay fresh with no rebuild.

Each roundup is a rule over live events plus an intro admins can edit. The intros live in app_settings under
INTROS_KEY as {slug: text}; a roundup without one uses its default.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from typing import Callable

from sqlalchemy import or_, select

from .categories import category_condition, label
from .config import get_settings
from .models import AppSetting, Event, Region, Venue
from .routers.events import _on_between, price_or_usual
from .seo import _page, path_for, price_text
from .visibility import live_condition

INTROS_KEY = "roundup_intros"
INTRO_MAX = 600
CHEAP_MAX = Decimal("25")  # "cheap" is $25 and under
CLOSING_DAYS = 14  # "closing soon" is the next two weeks, today included
SHOWN_MAX = 100  # a longer list ends with a link to the catalog
DAY_MAX = 40  # likewise for each day this weekend
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Roundup:
    slug: str
    group: str  # the heading it's listed under in "More roundups"
    name: str  # its link text
    h1: str
    title: str  # <title>, without " · Culture Crew"
    description: str
    intro: str  # the default intro
    empty: str  # the heading when nothing matches
    where: Callable[[date], list]  # extra conditions, given today
    closing: bool = False  # soonest to close first, rather than soonest on


def _side(region: Region):
    return lambda today: [Venue.region == region]


ROUNDUPS: tuple[Roundup, ...] = (
    Roundup("this-weekend", "When and price", "This weekend in Chicago", "This weekend in Chicago",
            "This weekend in Chicago: shows, concerts and exhibitions",
            "Theater, music and art on in Chicago this weekend, day by day, with times and prices.",
            "What’s on in Chicago this weekend: shows, concerts and exhibitions, day by day, with times and prices.",
            "Nothing is on the list for this weekend yet", lambda today: []),
    Roundup("free-and-cheap", "When and price", "Free and $25 and under", "Free and cheap things to do in Chicago",
            "Free and cheap things to do in Chicago",
            "Theater, music and art in Chicago for $25 or less, and plenty that’s free.",
            "Good nights out for $25 or less, and some for nothing at all. Prices are the lowest posted, so check the "
            "event’s own page before you go.",
            "Nothing $25 or under is on the list right now",
            lambda today: [price_or_usual() <= CHEAP_MAX]),
    Roundup("closing-soon", "When and price", "Closing soon: last chance", "Closing soon: last chance",
            "Closing soon in Chicago: last chance to see these",
            "Chicago shows and exhibitions that end in the next two weeks.",
            "Shows and exhibitions that end in the next two weeks. If one has been on your list, this is the time.",
            "Nothing is closing in the next two weeks",
            lambda today: [Event.end_date.is_not(None), Event.end_date <= today + timedelta(days=CLOSING_DAYS - 1)],
            closing=True),
    Roundup("theater", "Kind", "Theater", "Theater in Chicago", "Theater in Chicago: plays, musicals and opera",
            "Plays, musicals and opera on Chicago stages, soonest first, with dates and prices.",
            "Plays, musicals and opera on Chicago stages, soonest first.",
            "No theater is on the list right now", lambda today: [category_condition(["Theater"])]),
    Roundup("music", "Kind", "Music", "Music in Chicago", "Live music in Chicago: concerts and opera",
            "Concerts in Chicago, from orchestras and opera to jazz clubs, soonest first, with dates and prices.",
            "Concerts, from orchestras and opera to jazz clubs, soonest first.",
            "No music is on the list right now", lambda today: [category_condition(["Music"])]),
    Roundup("visual-art", "Kind", "Visual Art", "Visual art in Chicago", "Art exhibitions in Chicago",
            "Exhibitions at Chicago’s museums and galleries, soonest first, with dates and prices.",
            "Exhibitions at Chicago’s museums and galleries, soonest first.",
            "No exhibitions are on the list right now", lambda today: [category_condition(["Visual Art"])]),
    Roundup("downtown", "Side of town", "Downtown and the Loop", "Downtown and the Loop",
            "Things to do Downtown and in the Loop, Chicago",
            "Theater, music and art Downtown and in the Loop, soonest first.",
            "Theater, music and art Downtown and in the Loop, soonest first.",
            "Nothing Downtown is on the list right now", _side(Region.downtown)),
    Roundup("north-side", "Side of town", "North Side", "The North Side", "Things to do on Chicago’s North Side",
            "Theater, music and art on Chicago’s North Side, soonest first.",
            "Theater, music and art on the North Side, soonest first.",
            "Nothing on the North Side is on the list right now", _side(Region.north)),
    Roundup("west-side", "Side of town", "West Side", "The West Side", "Things to do on Chicago’s West Side",
            "Theater, music and art on Chicago’s West Side, soonest first.",
            "Theater, music and art on the West Side, soonest first.",
            "Nothing on the West Side is on the list right now", _side(Region.west)),
    Roundup("south-side", "Side of town", "South Side", "The South Side", "Things to do on Chicago’s South Side",
            "Theater, music and art on Chicago’s South Side, soonest first.",
            "Theater, music and art on the South Side, soonest first.",
            "Nothing on the South Side is on the list right now", _side(Region.south)),
    Roundup("suburbs", "Side of town", "Suburbs", "The suburbs", "Things to do in the Chicago suburbs",
            "Theater, music and art in the suburbs around Chicago, soonest first.",
            "Theater, music and art in the suburbs around Chicago, soonest first.",
            "Nothing in the suburbs is on the list right now", _side(Region.suburbs)),
)
BY_SLUG = {r.slug: r for r in ROUNDUPS}
WEEKEND = "this-weekend"


# ---- the rules -------------------------------------------------------------

def weekend_of(today: date) -> tuple[date, date]:
    """Friday to Sunday: this one from Friday on (starting today), else the coming one."""
    wd = today.weekday()
    if wd >= 4:
        return today, today + timedelta(days=6 - wd)
    friday = today + timedelta(days=4 - wd)
    return friday, friday + timedelta(days=2)


def _conditions(r: Roundup, today: date) -> list:
    conds = [live_condition(), or_(Event.end_date.is_(None), Event.end_date >= today), *r.where(today)]
    if r.slug == WEEKEND:
        first, last = weekend_of(today)
        # a start date is needed to say it's on this weekend; a fixed run only on its own dates (CC-96)
        conds += [Event.begin_date.is_not(None), Event.begin_date <= last, _on_between(first, last)]
    return conds


def _week_ok(weeks: dict, key: str, d: date) -> bool:
    """CC-88: a day that's only on some weeks of the month (1-4, 5 = the last)."""
    only = (weeks or {}).get(key)
    if not only:
        return True
    last = d.day + 7 > calendar.monthrange(d.year, d.month)[1]
    return (d.day - 1) // 7 + 1 in only or (5 in only and last)


def times_on(event: Event, d: date) -> list[str] | None:
    """The start times it's on at on day d ([] = on, time not known), or None when it isn't on that day."""
    if event.begin_date and d < event.begin_date or event.end_date and d > event.end_date:
        return None
    if event.run_dates:  # CC-96: a fixed run is only on its own dates
        return [r.time for r in event.run_dates if r.date == d and r.time] if any(r.date == d for r in event.run_dates) else None
    dated = [p.get("time") or "" for p in event.performances or [] if p.get("date") == d.isoformat()]
    if dated:
        return sorted(t for t in dated if t)
    key = WEEKDAYS[d.weekday()]
    venue = event.venue
    weekly = event.weekly or (venue.show_times if venue else {}) or {}
    if weekly:  # a performance: only on its show days
        if key in weekly and _week_ok(event.weeks if event.weekly else {}, key, d):
            return sorted(weekly[key] or [])
        return None
    hours = (venue.open_hours if venue else {}) or {}
    if key in hours and hours[key] is None:  # closed that day
        return None
    return []


def _clock(hhmm: str) -> str:
    """"19:30" -> "7:30 pm", "14:00" -> "2 pm"."""
    h, m = (int(x) for x in hhmm.split(":"))
    text = f"{h % 12 or 12}" + (f":{m:02d}" if m else "")
    return f"{text} {'am' if h < 12 else 'pm'}"


def _when_on(event: Event, d: date, times: list[str]) -> str:
    if times:
        return ", ".join(_clock(t) for t in times[:3])
    hours = ((event.venue.open_hours if event.venue else {}) or {}).get(WEEKDAYS[d.weekday()])
    if hours and len(hours) == 2:
        return f"{_clock(hours[0])}–{_clock(hours[1])}"
    return "All day"


def _short(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _long(d: date) -> str:
    return f"{d.strftime('%A')}, {_short(d)}"


def _when(event: Event, today: date, closing: bool) -> str:
    b, e = event.begin_date, event.end_date
    if closing and e:
        return "Last day today" if e == today else f"Closes {_short(e)}"
    if b and b > today:
        return f"Opens {_short(b)}"
    if e:
        return "Today only" if e == today else f"Through {_short(e)}"
    return "On now"


def _note(event: Event, today: date) -> str:
    """On the weekend page: when a run ends, flagged when that's soon."""
    e = event.end_date
    if e is None:
        return ""
    return f"Closes {_short(e)}" if (e - today).days < CLOSING_DAYS else f"Through {_short(e)}"


def _price(event: Event) -> str:
    text = price_text(event)
    if text == "Price not posted" and event.venue and event.venue.standard_cost.strip():
        return event.venue.standard_cost.strip()  # CC-89: the venue's usual cost
    return text


def matching(db, r: Roundup, today: date) -> list[Event]:
    """Its live events, soonest on first (or soonest to close, for Closing soon)."""
    rows = db.scalars(select(Event).join(Venue).where(*_conditions(r, today))).unique().all()
    if r.closing:
        key = lambda ev: (ev.end_date, ev.sort_title, ev.id)  # noqa: E731
    else:  # on now first (closing soonest), then by opening date; no start date last
        key = lambda ev: (ev.begin_date is None, max(ev.begin_date or today, today),  # noqa: E731
                          ev.end_date or date.max, ev.sort_title, ev.id)
    return sorted(rows, key=key)


def weekend_days(events: list[Event], today: date) -> list[tuple[date, list[tuple[Event, list[str]]]]]:
    """This weekend's days, each with the events whose first weekend day it is (an exhibition is listed once)."""
    first, last = weekend_of(today)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    by_day: dict[date, list] = {d: [] for d in days}
    for ev in events:
        for d in days:
            times = times_on(ev, d)
            if times is not None:
                by_day[d].append((ev, times))
                break
    for d in days:  # by start time, then all-day
        by_day[d].sort(key=lambda pair: (not pair[1], pair[1][:1], pair[0].sort_title))
    return [(d, by_day[d]) for d in days]


def count(db, r: Roundup, today: date) -> int:
    if r.slug == WEEKEND:
        return sum(len(evs) for _, evs in weekend_days(matching(db, r, today), today))
    return len(matching(db, r, today))


# ---- intros ----------------------------------------------------------------

def intros(db) -> dict[str, str]:
    row = db.get(AppSetting, INTROS_KEY)
    value = row.value if row and isinstance(row.value, dict) else {}
    return {k: v for k, v in value.items() if k in BY_SLUG and isinstance(v, str) and v.strip()}


def intro_of(db, r: Roundup) -> str:
    return intros(db).get(r.slug) or r.intro


def set_intro(db, slug: str, text: str) -> None:
    """An empty intro goes back to the default. The caller commits."""
    value = intros(db)
    if text.strip():
        value[slug] = text.strip()
    else:
        value.pop(slug, None)
    row = db.get(AppSetting, INTROS_KEY)
    if row is None:
        db.add(AppSetting(key=INTROS_KEY, value=value))
    else:
        row.value = value  # a new dict, so SQLAlchemy sees the change


# ---- pages -----------------------------------------------------------------

ROUNDUP_STYLE = """
:root, :root[data-theme="dark"] { --cat-theater: #FDA15C; --cat-music: #C9A2FB; --cat-art: #5FD9A8; }
:root[data-theme="light"] { --cat-theater: #B84E05; --cat-music: #7A2BC8; --cat-art: #08795A; }
.wrap { max-width: 880px; }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.navlink { display: inline-flex; align-items: center; min-height: 44px; font-weight: 600; text-decoration: none; }
.meta { margin: 0 0 20px; color: var(--muted); font-size: 1.0625rem; }
.intro { margin: 0 0 28px; font-size: 1.125rem; max-width: 640px; }
.intro p { margin: 0 0 12px; }
.daynav { display: flex; gap: 10px; flex-wrap: wrap; margin: 0 0 40px; }
.daynav a, .pill { display: inline-flex; align-items: center; min-height: 44px; padding: 0 16px; border: 1px solid var(--line);
  border-radius: 999px; background: var(--surface); color: var(--text); font-weight: 600; text-decoration: none; }
.day { margin: 0 0 40px; scroll-margin-top: 16px; }
.day h2, .more h2, .group h2 { margin: 0 0 14px; font: 600 1.5rem/1.2 "Outfit", "Figtree", system-ui, sans-serif; }
.rows { list-style: none; margin: 0 0 40px; padding: 0; display: flex; flex-direction: column; gap: 12px; }
.day .rows { margin: 0; }
.row { display: grid; grid-template-columns: 112px minmax(0, 1fr) auto; gap: 20px; align-items: start; padding: 18px 20px;
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px; color: var(--text); text-decoration: none; }
.row:hover { border-color: var(--muted); }
.when, .price { font-weight: 600; font-variant-numeric: tabular-nums; }
.price { text-align: right; }
.what { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.cat { display: inline-flex; align-items: center; gap: 8px; font-size: 0.8125rem; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; }
.cat::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
.cat-theater { color: var(--cat-theater); } .cat-music { color: var(--cat-music); } .cat-art { color: var(--cat-art); }
.name { font: 600 1.3125rem/1.25 "Outfit", "Figtree", system-ui, sans-serif; overflow-wrap: anywhere; }
.where, .small { color: var(--muted); }
.small { font-size: 0.875rem; }
.panel { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; padding: 24px;
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px; }
.panel p { margin: 0; font-size: 1.0625rem; }
.empty { flex-direction: column; align-items: flex-start; margin: 0 0 40px; }
.empty h2 { margin: 0; font: 600 1.3125rem/1.25 "Outfit", "Figtree", system-ui, sans-serif; }
.more { margin-top: 56px; padding-top: 28px; border-top: 1px solid var(--line); }
.more .cols { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 24px; }
.more ul, .group ul { list-style: none; margin: 0; padding: 0; }
.more li a, .group li a { display: inline-flex; align-items: center; min-height: 36px; font-weight: 500; text-decoration: none; }
.group { margin: 0 0 32px; }
.group .count { color: var(--muted); font-weight: 400; }
@media (max-width: 600px) {
  .row { grid-template-columns: 1fr auto; gap: 4px 12px; padding: 16px; }
  .what { grid-column: 1 / -1; grid-row: 1; }
  .when, .price { grid-row: 2; margin-top: 4px; }
  .name { font-size: 1.1875rem; }
  .panel { flex-direction: column; align-items: stretch; }
  .panel .btn { justify-content: center; }
  .more .cols { grid-template-columns: 1fr; gap: 16px; }
  .more li { display: inline; margin-right: 18px; }
  .more li a { min-height: 44px; }
}
"""

_CAT_CLASS = {"Theater": "cat-theater", "Music": "cat-music", "Visual Art": "cat-art"}


def _e(s: str) -> str:
    return escape(s, quote=True)


def _cat_class(event: Event) -> str:
    return "cat-music" if event.opera else _CAT_CLASS.get(event.category, "")


def _row(event: Event, when: str, note: str = "") -> str:
    site = get_settings().site_url
    note_html = f'<span class="small">{_e(note)}</span>' if note else ""
    where = event.venue.name + (f" · {event.venue.neighborhood.strip()}" if event.venue.neighborhood.strip() else "")
    return (f'<li><a class="row" href="{_e(site + path_for(event))}">'
            f'<span class="when">{_e(when)}</span>'
            f'<span class="what"><span class="cat {_cat_class(event)}">{_e(label(event.category, event.opera))}</span>'
            f'<span class="name">{_e(event.title)}</span><span class="where">{_e(where)}</span>{note_html}</span>'
            f'<span class="price">{_e(_price(event))}</span></a></li>')


def _more_html(current: str | None) -> str:
    site = get_settings().site_url
    cols = []
    for group in dict.fromkeys(r.group for r in ROUNDUPS):
        links = "".join(f'<li><a href="{_e(site)}/chicago/{r.slug}"'
                        + (' aria-current="page"' if r.slug == current else "") + f">{_e(r.name)}</a></li>"
                        for r in ROUNDUPS if r.group == group)
        cols.append(f'<div><p class="eyebrow">{_e(group)}</p><ul>{links}</ul></div>')
    return (f'<footer class="more" aria-labelledby="more-h"><h2 id="more-h">More roundups</h2>'
            f'<div class="cols">{"".join(cols)}</div></footer>')


def _nav() -> str:
    return f'<a class="navlink" href="{_e(get_settings().site_url)}/#catalog">Browse the catalog</a>'


def _intro_html(text: str) -> str:
    paras = [p.strip() for p in text.replace("\r", "").split("\n\n") if p.strip()]
    return '<div class="intro">' + "".join(f"<p>{_e(p)}</p>" for p in paras) + "</div>"


def _catalog_panel(lead: str) -> str:
    return (f'<div class="panel"><p>{_e(lead)}</p>'
            f'<a class="btn primary" href="{_e(get_settings().site_url)}/#catalog">Browse the catalog</a></div>')


def _count_text(n: int) -> str:
    return f"{n} event" + ("" if n == 1 else "s")


def render_roundup(db, r: Roundup, today: date) -> str:
    site = get_settings().site_url
    events = matching(db, r, today)
    body = ['<p class="eyebrow">Chicago roundup</p>', f"<h1>{_e(r.h1)}</h1>"]
    if r.slug == WEEKEND:
        days = weekend_days(events, today)
        total = sum(len(evs) for _, evs in days)
        span = _long(days[0][0]) + (f" to {_long(days[-1][0])}" if len(days) > 1 else "")
        body.append(f'<p class="meta">{_e(span)} · {_count_text(total)}</p>')
    else:
        total = len(events)
        meta = _count_text(total)
        if r.closing:
            meta = f"Ending by {_long(today + timedelta(days=CLOSING_DAYS - 1))} · {meta}"
        body.append(f'<p class="meta">{_e(meta)}</p>')
    body.append(_intro_html(intro_of(db, r)))

    shown = 0
    if total == 0:
        body.append(f'<div class="panel empty"><h2>{_e(r.empty)}</h2>'
                    '<p>The catalog has everything else, soonest first.</p>'
                    f'<a class="btn primary" href="{_e(site)}/#catalog">Browse the catalog</a></div>')
    elif r.slug == WEEKEND:
        sections = []
        for d, evs in days:
            if evs:  # each day gets its share, so a busy Friday can't crowd out Sunday
                rows = [_row(ev, _when_on(ev, d, times), _note(ev, today)) for ev, times in evs[:DAY_MAX]]
                sections.append((d, rows))
                shown += len(rows)
        if len(sections) > 1:
            body.append('<nav class="daynav" aria-label="Days">' + "".join(
                f'<a href="#{d.strftime("%a").lower()}">{d.strftime("%A")}</a>' for d, _ in sections) + "</nav>")
        body += [f'<section class="day" id="{d.strftime("%a").lower()}" aria-labelledby="h-{d.isoformat()}">'
                 f'<h2 id="h-{d.isoformat()}">{_e(_long(d))}</h2><ol class="rows">{"".join(rows)}</ol></section>'
                 for d, rows in sections]
    else:
        rows = [_row(ev, _when(ev, today, r.closing)) for ev in events[:SHOWN_MAX]]
        shown = len(rows)
        body.append(f'<ol class="rows">{"".join(rows)}</ol>')

    if total > shown:
        body.append(_catalog_panel(f"That’s {shown} of {total}. The catalog has them all, with filters."))
    elif total:
        body.append(_catalog_panel("Want more? The catalog has every event, with filters for price, day and side of town."))
    body.append('<p class="note">Dates and prices change, so check the event’s own page before you go.</p>')
    body.append(_more_html(r.slug))
    return _page(f"{r.title} · Culture Crew", r.description, f"{site}/chicago/{r.slug}", "\n".join(body),
                 f"<style>{ROUNDUP_STYLE}</style>", nav=_nav())


def render_index(db, today: date) -> str:
    site = get_settings().site_url
    body = ['<p class="eyebrow">Chicago roundups</p>', "<h1>What’s on in Chicago</h1>",
            '<div class="intro"><p>Theater, music and art in Chicago, gathered by when, price, kind and side of town. '
            "Each list keeps itself up to date from the catalog.</p></div>"]
    for group in dict.fromkeys(r.group for r in ROUNDUPS):
        links = "".join(f'<li><a href="{_e(site)}/chicago/{r.slug}">{_e(r.name)}</a> '
                        f'<span class="count">· {_count_text(count(db, r, today))}</span></li>'
                        for r in ROUNDUPS if r.group == group)
        body.append(f'<section class="group"><h2>{_e(group)}</h2><ul>{links}</ul></section>')
    body.append(_catalog_panel("The catalog has every event, with filters for price, day and side of town."))
    return _page("What’s on in Chicago: roundups · Culture Crew",
                 "Chicago theater, music and art by when, price, kind and side of town: this weekend, free and cheap, "
                 "closing soon and more.", f"{site}/chicago/", "\n".join(body), f"<style>{ROUNDUP_STYLE}</style>",
                 nav=_nav())


def render_unknown() -> str:
    site = _e(get_settings().site_url)
    body = ('<h1>That roundup isn’t here</h1>\n<p class="place">The link may be wrong, or the page has moved.</p>\n'
            f'<div class="actions"><a class="btn primary" href="{site}/chicago/">See all roundups</a></div>')
    return _page("Roundup not found · Culture Crew", "That roundup isn't on Culture Crew.", None, body,
                 f"<style>{ROUNDUP_STYLE}</style>", noindex=True, nav=_nav())


def sitemap_paths(db, today: date) -> list[str]:
    """The index, plus each roundup that has events right now (an empty one isn't worth a search result)."""
    return ["/chicago/"] + [f"/chicago/{r.slug}" for r in ROUNDUPS if count(db, r, today)]
