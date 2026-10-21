"""The weekly discovery recap (CC-77, designed in CC-81): Tuesdays at 10 AM Chicago time, to settings.recap_to.
An HTML email in the site's daytime colors (option B, "at a glance"), with a plain-text version as the
fallback. It covers the past seven days:

- four numbers at the top: waiting for review, new this week, proposed updates, to check by hand
- new events found, by source; proposed updates (prices, dates), each a table
- what was left out automatically (by your skip rules and the built-in rules), each with a link to that list
  on Found events, where any of them can still be approved
- sources that couldn't be read, each with a "Check by hand only" link, and the sources already checked by hand
- the week's checks and cost, this month against the budget, and every source with how often it's checked

It's sent even when the week was quiet ("Nothing new this week"), so a missing email means something
is wrong. Links are named links to the site (Found events, Event sources), never written-out addresses.
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html import escape

from sqlalchemy import func, select

from ..config import get_settings
from ..mail import Email
from ..models import DiscoveredItem, DiscoveryCheck, DiscoverySource, IssueReport, WentPhoto
from ..stats import CHICAGO
from .budget import budget_state
from .needs import needs
from .readers import cost_usd
from .store import venue_for

DAYS = 7
CHANGE_NAMES = {"cost_text": "Price", "min_price": "Lowest price", "begin_date": "Opens", "end_date": "Closes", "weekly": "Show days", "weeks": "Weeks of the month"}
EVERY = {7: "Every week", 14: "Every 2 weeks", 30: "Every month", 60: "Every 2 months"}
MAX_UPDATES = 20
# the site's daytime ("Paper and signal") colors
PAGE, SURFACE, LINE, TEXT, MUTED, LEAD, LINK = "#F6F6F3", "#FFFFFF", "#E2E2DC", "#111111", "#5C5C57", "#D62839", "#1D4ED8"
LEAD_SOFT, ON_LEAD_SOFT = "#FBE4E6", "#9E1B28"
FONT = "Figtree, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
DISPLAY = "Outfit, Figtree, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"


def _md(d: date) -> str:
    return f"{d:%b} {d.day}"


def _change(field_name: str, v) -> str:
    if v in (None, ""):
        return "none"
    if field_name in ("begin_date", "end_date"):
        return _md(datetime.fromisoformat(v))
    if field_name == "min_price":
        return "free" if v == 0 else f"${v:g}"
    if field_name == "weekly":
        return show_days_text(v)
    if field_name == "weeks":
        return "; ".join(f"{d.capitalize()}: " + " and ".join(ORDINALS[w] for w in ws) for d, ws in v.items()) or "every week"
    return str(v)


def _clock(t: str) -> str:
    h, m = map(int, t.split(":"))
    return f"{(h % 12) or 12}{f':{m:02d}' if m else ''} {'AM' if h < 12 else 'PM'}"


ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "last"}


def show_days_text(weekly: dict, weeks: dict | None = None) -> str:
    """{"thu": ["20:00"], "fri": ["20:00", "22:30"]} -> "Thu 8 PM; Fri 8 PM and 10:30 PM" (CC-88); with weeks
    {"thu": [1, 3]}, "1st and 3rd Thu 8 PM; ..."."""
    def day(d: str) -> str:
        on = (weeks or {}).get(d)
        return (" and ".join(ORDINALS[w] for w in on) + " " if on else "") + d.capitalize()
    return "; ".join(f"{day(d)} " + " and ".join(map(_clock, ts)) for d, ts in weekly.items() if ts) or "none"


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _aware(ts: datetime | None) -> datetime | None:
    return ts and (ts if ts.tzinfo else ts.replace(tzinfo=UTC))  # SQLite hands back naive UTC


@dataclass
class Week:
    """Everything the recap says, gathered once and written out as HTML and as text."""
    first: date
    last: date
    site: str
    name: str
    new_waiting: int = 0
    updates_waiting: int = 0
    found_by_source: list[tuple[str, int]] = field(default_factory=list)
    found_total: int = 0
    updates: list[tuple[str, str, str, str, str]] = field(default_factory=list)  # event, source, change, was, now
    more_updates: int = 0
    by_rule: list[tuple[str, int]] = field(default_factory=list)
    built_in: list[tuple[str, int]] = field(default_factory=list)
    unreadable: list[tuple[int, str, str, str, str]] = field(default_factory=list)  # id, name, page, reason, last tried
    by_hand: list[tuple[str, str]] = field(default_factory=list)  # name, page
    checks: int = 0
    pages: int = 0
    week_cost: float = 0
    state: str = "ok"
    spent: float = 0
    budget: float = 10
    sources: list[tuple[str, str, str]] = field(default_factory=list)  # name, how often, next check
    issues: list[tuple[str, str, str, str]] = field(default_factory=list)  # CC-84: reported this week: ticket, who, page, what happened
    issues_open: int = 0  # new or being looked into, all time
    photos_waiting: int = 0  # CC-91: "I've been" photos waiting for an approver
    needs: int = 0  # CC-88: new events still missing a date or show days

    @property
    def quiet(self) -> bool:
        return not (self.found_total or self.updates or self.by_rule or self.built_in or self.unreadable or self.issues) and self.state == "ok"

    def link(self, where: str) -> str:
        return f"{self.site}/#{where}"


def gather(db, now: datetime) -> Week:
    s = get_settings()
    since = now - timedelta(days=DAYS)
    w = Week(first=since.astimezone(CHICAGO).date(), last=(now.astimezone(CHICAGO) - timedelta(days=1)).date(),
             site=s.site_url.rstrip("/"), name=s.recap_name.strip())
    sources = db.scalars(select(DiscoverySource).order_by(func.lower(DiscoverySource.name))).all()
    names = {src.id: src.name for src in sources}

    week = db.scalars(select(DiscoveredItem).where(DiscoveredItem.first_seen_at >= since)).all()
    rule_caught = [i for i in week if i.status == "rejected" and i.skip_reason.startswith("skip rule")]
    found = [i for i in week if i.status in ("new", "approved", "rejected") and i not in rule_caught]
    w.found_total = len(found)
    w.found_by_source = Counter(names.get(i.source_id, "?") for i in found).most_common()
    w.by_rule = Counter(i.skip_reason.removeprefix("skip rule: ").strip("\u201c\u201d") for i in rule_caught).most_common()
    w.built_in = Counter((i.skip_reason or "other").capitalize() for i in week if i.status == "skipped").most_common()

    for i in db.scalars(select(DiscoveredItem).where(DiscoveredItem.status == "update").order_by(DiscoveredItem.first_seen_at)):
        for k, (was, now_v) in (i.changes or {}).items():
            if k == "min_price" and "cost_text" in i.changes:
                continue  # the price as written says it better
            w.updates.append((i.title, names.get(i.source_id, "?"), CHANGE_NAMES.get(k, k), _change(k, was), _change(k, now_v)))
    w.more_updates, w.updates = max(len(w.updates) - MAX_UPDATES, 0), w.updates[:MAX_UPDATES]

    waiting = dict(db.execute(select(DiscoveredItem.status, func.count()).where(DiscoveredItem.status.in_(["new", "update"]))
                              .group_by(DiscoveredItem.status)).all())
    w.new_waiting, w.updates_waiting = waiting.get("new", 0), waiting.get("update", 0)
    w.needs = sum(1 for i in db.scalars(select(DiscoveredItem).where(DiscoveredItem.status == "new"))
                  if needs(i, venue_for(db, i.source, i)))

    for src in sources:
        if not src.active:
            continue
        page = src.pages[0] if src.pages else ""
        if src.manual:
            w.by_hand.append((src.name, page))
        elif src.last_status in ("blocked", "error"):
            at = _aware(src.last_checked_at)
            w.unreadable.append((src.id, src.name, page, src.last_error or src.last_status,
                                 f"{at.astimezone(CHICAGO):%a} {_md(at.astimezone(CHICAGO))}" if at else ""))
        how = EVERY.get(src.every_days, f"Every {src.every_days} days")
        nxt = "By hand" if src.manual else _md(src.next_check_on) if src.next_check_on else "Within the hour"
        w.sources.append((src.name, "By hand" if src.manual else how, nxt))

    checks, pages, tin, tout = db.execute(select(func.count(), func.coalesce(func.sum(DiscoveryCheck.pages_read), 0),
                                                 func.coalesce(func.sum(DiscoveryCheck.input_tokens), 0),
                                                 func.coalesce(func.sum(DiscoveryCheck.output_tokens), 0))
                                          .where(DiscoveryCheck.checked_at >= since)).one()
    w.checks, w.pages, w.week_cost = checks, int(pages), cost_usd(int(tin), int(tout))
    w.state, w.spent, w.budget = budget_state(db, now)
    for i in db.scalars(select(IssueReport).where(IssueReport.created_at >= since).order_by(IssueReport.created_at)):
        who = (i.reporter.display_name.strip() if i.reporter else "") or ("A member" if i.reporter else "A deleted account")
        what = i.what_happened if len(i.what_happened) <= 140 else i.what_happened[:137].rstrip() + "..."
        w.issues.append((i.key, who, i.page or "-", what))
    w.photos_waiting = db.scalar(select(func.count()).select_from(WentPhoto).where(WentPhoto.status == "waiting")) or 0
    w.issues_open = db.scalar(select(func.count()).select_from(IssueReport).where(IssueReport.status.in_(["new", "looking"]))) or 0
    return w


def subject_of(w: Week) -> str:
    if w.quiet:
        return "Event discovery: nothing new this week"
    bits = [f"{w.found_total} new"] + ([f"{len(w.updates)} {'update' if len(w.updates) == 1 else 'updates'}"] if w.updates else []) \
        + ([f"{len(w.unreadable)} to check by hand"] if w.unreadable else []) \
        + ([f"{len(w.issues)} {'issue' if len(w.issues) == 1 else 'issues'} reported"] if w.issues else []) \
        + (["budget used up"] if w.state == "paused" else [])
    return "Event discovery: " + ", ".join(bits)


def budget_note(w: Week) -> str:
    if w.state == "saving":
        return "Over 80% of the budget: event pages wait until next month. Sources are still checked."
    if w.state == "paused":
        return "The budget is used up: only Check now runs until next month. Raise it on Event sources if needed."
    return ""


# ---- plain text (the fallback) -------------------------------------------------------

def _cols(rows: list[tuple], indent: str = "  ") -> list[str]:
    widths = [max(len(str(r[c])) for r in rows) for c in range(len(rows[0]))] if rows else []
    return [indent + "  ".join(str(v).ljust(widths[c]) for c, v in enumerate(r)).rstrip() for r in rows]


def as_text(w: Week) -> str:
    hello = f"Hello, {w.name}!" if w.name else "Hello!"
    lines = [f"{hello} Here's what's changed in the last week ({_md(w.first)} to {_md(w.last)}).", "",
             f"Waiting for review: {w.new_waiting + w.updates_waiting} ({w.new_waiting} new, {w.updates_waiting} proposed updates)",
             f"Review found events: {w.link('found')}", ""]
    if w.needs:
        lines[-1:-1] = [f"{_n(w.needs, 'event')} {'needs' if w.needs == 1 else 'need'} a date or show days. "
                        f"We read their pages again each week for up to 4 weeks: {w.link('found/needs')}"]
    if w.quiet:
        lines += ["Nothing new this week: no events found, nothing left out, and every source could be read.", ""]
    lines += [f"NEW EVENTS FOUND THIS WEEK: {w.found_total}"] + (_cols(w.found_by_source) or ["  None."]) + [""]
    lines += [f"PROPOSED UPDATES: {len(w.updates) + w.more_updates}"]
    lines += _cols([(f"{e} ({src})", ch, f"{was} -> {now}") for e, src, ch, was, now in w.updates]) or ["  None."]
    lines += [f"  ...and {w.more_updates} more."] if w.more_updates else []
    lines += ["", f"LEFT OUT AUTOMATICALLY: {sum(n for _, n in w.by_rule + w.built_in)}"]
    if w.by_rule:
        lines += ["  By your skip rules:"] + _cols([(f"“{p}”", n) for p, n in w.by_rule], "    ") + [f"    Review: {w.link('found/rejected')}"]
    if w.built_in:
        lines += ["  By the built-in rules:"] + _cols(w.built_in, "    ") + [f"    Review: {w.link('found/skipped')}"]
    lines += ["  None."] if not (w.by_rule or w.built_in) else []
    lines += ["", f"COULDN'T BE READ: {len(w.unreadable)}"]
    for sid, name, page, reason, tried in w.unreadable:
        lines += [f"  - {name}: {reason}" + (f" (last tried {tried})" if tried else ""), f"    Check by hand only: {w.link(f'source-{sid}')}"]
    lines += ["  None. Every source could be read."] if not w.unreadable else []
    if w.by_hand:
        lines += ["", "CHECK THESE BY HAND:"] + [f"  - {name}: {page}" for name, page in w.by_hand]
    lines += ["", f"ISSUES REPORTED THIS WEEK: {len(w.issues)} ({w.issues_open} open in all)"]
    lines += [f"  - {key} {who} on {page}: {what}" for key, who, page, what in w.issues] or ["  None."]
    lines += [f"  Review them: {w.link('issues')}"] if w.issues or w.issues_open else []
    lines += ["", f"PHOTOS WAITING FOR APPROVAL: {w.photos_waiting}"] + ([f"  Approve them: {w.link('approvals')}"] if w.photos_waiting else [])
    lines += ["", f"CHECKS THIS WEEK: {_n(w.checks, 'check')}, {_n(w.pages, 'page')} read, about ${w.week_cost:.2f}",
              f"This month: ${w.spent:.2f} of the ${w.budget:.0f} budget." + (f" {budget_note(w)}" if budget_note(w) else "")]
    lines += _cols([("Source", "How often", "Next check")] + w.sources)
    lines += ["", f"Event sources: {w.link('sources')}", "Sent every Tuesday at 10 AM.", "Culture Crew"]
    return "\n".join(lines) + "\n"


# ---- HTML (option B, "at a glance") ---------------------------------------------------

def _a(href: str, text: str, color: str = LINK) -> str:
    return f'<a href="{escape(href)}" style="color:{color};text-decoration:underline">{escape(text)}</a>'


def _h(text: str, extra: str = "") -> str:
    return (f'<h2 style="margin:28px 0 8px;font-family:{DISPLAY};font-weight:600;font-size:17px;line-height:1.3;color:{TEXT}">'
            f'{escape(text)}{extra}</h2>')


def _table(head: list[str], rows: list[list[str]], numeric: set[int] = frozenset(), caption: str = "") -> str:
    """A striped data table. Cells are HTML (already escaped by the caller)."""
    def cell(tag, i, v, bg):
        align = "right" if i in numeric else "left"
        style = (f"padding:7px 10px;text-align:{align};font-size:13px;line-height:1.4;color:{TEXT};background:{bg};"
                 + ("font-variant-numeric:tabular-nums;" if i in numeric else ""))
        if tag == "th":
            style = (f"padding:6px 10px;text-align:{align};font-size:11px;font-weight:600;letter-spacing:.06em;"
                     f"text-transform:uppercase;color:{MUTED};border-bottom:1px solid {LINE};background:{SURFACE}")
            return f'<th scope="col" style="{style}">{v}</th>'
        return f'<td style="{style}">{v}</td>'
    out = [f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%">']
    if caption:
        out.append(f'<caption style="text-align:left;font-size:12px;color:{MUTED};padding:0 0 4px">{escape(caption)}</caption>')
    out.append("<tr>" + "".join(cell("th", i, escape(h), SURFACE) for i, h in enumerate(head)) + "</tr>")
    for r, row in enumerate(rows):
        bg = PAGE if r % 2 else SURFACE
        out.append("<tr>" + "".join(cell("td", i, v, bg) for i, v in enumerate(row)) + "</tr>")
    return "".join(out) + "</table>"


def _tile(n: int, label: str, strong: bool = False) -> str:
    bg, fg, sub = (LEAD_SOFT, ON_LEAD_SOFT, ON_LEAD_SOFT) if strong else (PAGE, TEXT, MUTED)
    return (f'<td width="25%" style="background:{bg};border-radius:10px;padding:12px 6px;text-align:center;vertical-align:top">'
            f'<div style="font-family:{DISPLAY};font-weight:600;font-size:24px;line-height:1.1;color:{fg}">{n}</div>'
            f'<div style="font-size:12px;line-height:1.3;color:{sub};margin-top:2px">{escape(label)}</div></td>')


def _none(text: str) -> str:
    return f'<p style="margin:0;font-size:14px;color:{MUTED}">{escape(text)}</p>'


def as_html(w: Week) -> str:
    hello = f"Hello, {w.name}!" if w.name else "Hello!"
    to_check = len(w.unreadable) + len(w.by_hand)
    parts = [
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="font-family:{DISPLAY};font-weight:600;font-size:15px;color:{TEXT}"><span style="color:{LEAD}">&#10038;</span> Culture Crew</td>'
        f'<td style="text-align:right;font-size:12px;color:{MUTED}">Weekly discovery &middot; {_md(w.first)}&ndash;{_md(w.last)}</td></tr></table>',
        f'<h1 style="margin:16px 0 0;font-family:{DISPLAY};font-weight:600;font-size:22px;line-height:1.25;color:{TEXT}">'
        f'{escape(hello)} Here\'s what\'s changed in the last week.</h1>',
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="8" style="margin:12px -8px 0;width:calc(100% + 16px)"><tr>'
        + _tile(w.new_waiting + w.updates_waiting, "waiting for review", strong=True) + _tile(w.found_total, "new this week")
        + _tile(w.updates_waiting, "proposed updates") + _tile(to_check, "to check by hand") + "</tr></table>",
        f'<p style="margin:12px 0 0"><a href="{escape(w.link("found"))}" style="display:inline-block;background:{LEAD};color:#FFFFFF;'
        f'text-decoration:none;font-weight:600;font-size:14px;padding:10px 18px;border-radius:8px">Review found events</a></p>',
    ]
    if w.needs:
        parts.append(f'<p style="margin:12px 0 0;font-size:14px"><b>{_n(w.needs, "event")} {"needs" if w.needs == 1 else "need"} '
                     f'a date or show days.</b> We read their pages again each week for up to 4 weeks. '
                     f'{_a(w.link("found/needs"), "Review them")}</p>')
    if w.quiet:
        parts.append(f'<p style="margin:16px 0 0;font-size:14px">Nothing new this week: no events found, nothing left out, '
                     f'and every source could be read.</p>')

    parts.append(_h("New events found this week"))
    parts.append(_table(["Source", "New"], [[escape(s), str(n)] for s, n in w.found_by_source], {1}) if w.found_by_source
                 else _none("None this week."))

    parts.append(_h("Proposed updates"))
    if w.updates:
        parts.append(_table(["Event", "Change", "From → to"],
                            [[f'{escape(e)} <span style="color:{MUTED}">&middot; {escape(src)}</span>', escape(ch),
                              f'{escape(was)} &rarr; <b>{escape(now)}</b>'] for e, src, ch, was, now in w.updates]))
        if w.more_updates:
            parts.append(f'<p style="margin:6px 0 0;font-size:13px;color:{MUTED}">And {w.more_updates} more on '
                         f'{_a(w.link("found/update"), "Found events")}.</p>')
    else:
        parts.append(_none("None waiting."))

    parts.append(_h("Left out automatically"))
    rows = [[f"Your skip rule “{escape(p)}”", str(n), _a(w.link("found/rejected"), "Review")] for p, n in w.by_rule] \
        + [[escape(why), str(n), _a(w.link("found/skipped"), "Review")] for why, n in w.built_in]
    parts.append(_table(["Why", "Events", ""], rows, {1, 2}) if rows else _none("Nothing this week."))

    parts.append(_h("Couldn't be read"))
    if w.unreadable:
        items = "".join(f'<li style="margin:0 0 6px">{_a(page or w.link("sources"), name)}: {escape(reason)}'
                        + (f' <span style="color:{MUTED}">(last tried {escape(tried)})</span>' if tried else "")
                        + f' &middot; {_a(w.link(f"source-{sid}"), "Check by hand only")}</li>'
                        for sid, name, page, reason, tried in w.unreadable)
        parts.append(f'<ul style="margin:0;padding-left:18px;font-size:14px;line-height:1.45">{items}</ul>')
    else:
        parts.append(_none("Every source could be read."))
    if w.by_hand:
        items = "".join(f'<li style="margin:0 0 4px">{_a(page or w.link("sources"), name)}</li>' for name, page in w.by_hand)
        parts.append(f'<p style="margin:12px 0 4px;font-size:14px;font-weight:600">Check these by hand</p>'
                     f'<ul style="margin:0;padding-left:18px;font-size:14px;line-height:1.45">{items}</ul>')

    parts.append(_h("Issues reported this week", f' <span style="font-family:{FONT};font-weight:400;font-size:13px;color:{MUTED}">'
                    f'&middot; {w.issues_open} open in all</span>'))
    parts.append(_table(["Ticket", "Reported by", "Page", "What happened"],
                        [[_a(w.link("issues/" + k), k), escape(a), escape(b), escape(c)] for k, a, b, c in w.issues])
                 if w.issues else _none("None this week."))
    if w.issues or w.issues_open:
        parts.append(f'<p style="margin:8px 0 0;font-size:13px">{_a(w.link("issues"), "Review issues")}</p>')

    parts.append(_h("Photos waiting for approval"))  # CC-91
    parts.append(f'<p style="margin:0;font-size:14px">{_n(w.photos_waiting, "photo")} waiting. {_a(w.link("approvals"), "Approve them")}</p>'
                 if w.photos_waiting else _none("None waiting."))

    parts.append(_h("Checks this week"))
    note = budget_note(w)
    parts.append(f'<p style="margin:0 0 8px;font-size:13px;color:{MUTED}">{_n(w.checks, "check")} &middot; {_n(w.pages, "page")} read &middot; '
                 f'about ${w.week_cost:.2f} &middot; ${w.spent:.2f} of the ${w.budget:.0f} monthly budget</p>'
                 + (f'<p style="margin:0 0 8px;padding:8px 10px;border-radius:8px;background:{LEAD_SOFT};color:{ON_LEAD_SOFT};'
                    f'font-size:13px">{escape(note)}</p>' if note else ""))
    parts.append(_table(["Source", "How often", "Next check"], [[escape(a), escape(b), escape(c)] for a, b, c in w.sources]))
    parts.append(f'<p style="margin:24px 0 0;font-size:12px;color:{MUTED}">{_a(w.link("sources"), "Event sources")} '
                 f'&middot; Sent every Tuesday at 10 AM</p>')

    body = "".join(parts)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">'
            f'<title>{escape(subject_of(w))}</title>'
            f'<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;600&amp;family=Outfit:wght@600&amp;display=swap" rel="stylesheet">'
            f'</head><body style="margin:0;padding:0;background:{PAGE}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE}"><tr><td style="padding:20px 12px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto;'
            f'background:{SURFACE};border:1px solid {LINE};border-radius:12px"><tr><td style="padding:24px 26px;'
            f'font-family:{FONT};color:{TEXT};line-height:1.5">{body}</td></tr></table>'
            f'</td></tr></table></body></html>')


def build_recap(db, now: datetime | None = None) -> Email:
    w = gather(db, now or datetime.now(UTC))
    return Email(to=get_settings().recap_to, subject=subject_of(w), text=as_text(w), html=as_html(w))
