"""Fetching venues' pages politely.

- Every request says who we are (USER_AGENT) and has a timeout and a size cap.
- robots.txt is followed for our user agent, and a site that disallows AI crawlers (ClaudeBot,
  anthropic-ai and the like) is treated as off limits too: its pages would be read by Claude.
- Pages are fetched with If-None-Match / If-Modified-Since, so an unchanged page usually costs a
  tiny 304 reply and nothing else.
- Requests to one site are spaced out (MIN_GAP_SECONDS, or the site's own Crawl-delay).
There is no logging in, no pretending to be a browser and no retrying around a refusal: a refused
page is reported as blocked, for a person to check by hand.
"""
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

USER_AGENT = "CultureCrewBot/1.0 (+https://chiculturecrew.com; info@chiculturecrew.com)"
AI_AGENTS = {"claudebot", "anthropic-ai", "claude-web", "claude-searchbot"}
TIMEOUT = 25
MAX_BYTES = 6_000_000
MIN_GAP_SECONDS = 3

_robots: dict[str, "Robots"] = {}
_last_hit: dict[str, float] = {}


class Blocked(Exception):
    """The site doesn't allow us (robots.txt, an AI opt-out, or a refusal such as 403)."""


class FetchError(Exception):
    """Couldn't get the page this time (network, timeout, 5xx). Worth trying again later."""


@dataclass
class Page:
    url: str
    status: int  # 200, or 304 when unchanged
    text: str = ""
    etag: str = ""
    last_modified: str = ""
    content_type: str = ""


@dataclass
class Robots:
    disallow: list[str]
    allow: list[str]
    crawl_delay: float
    ai_opt_out: bool

    def allows(self, path: str) -> bool:
        best_allow = max((len(p) for p in self.allow if path.startswith(p)), default=-1)
        best_disallow = max((len(p) for p in self.disallow if p and path.startswith(p)), default=-1)
        return best_allow >= best_disallow


def parse_robots(text: str) -> Robots:
    """The rules for us (the '*' group, as we have no group of our own), whether AI crawlers are
    shut out entirely, and any Crawl-delay."""
    groups: list[tuple[set[str], list[tuple[str, str]]]] = []
    agents: set[str] = set()
    rules: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (x.strip() for x in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if rules:
                groups.append((agents, rules)); agents, rules = set(), []
            agents.add(value.lower())
        elif field in ("allow", "disallow", "crawl-delay"):
            rules.append((field, value))
    if agents or rules:
        groups.append((agents, rules))
    mine = [r for a, r in groups if "*" in a or "culturecrewbot" in a]
    flat = [r for group in mine for r in group]
    ai_opt_out = any(a & AI_AGENTS and any(f == "disallow" and v.strip() == "/" for f, v in r) for a, r in groups)
    delay = max((float(v) for f, v in flat if f == "crawl-delay" and v.replace(".", "", 1).isdigit()), default=0.0)
    return Robots(disallow=[v for f, v in flat if f == "disallow"], allow=[v for f, v in flat if f == "allow"],
                  crawl_delay=min(delay, 30.0), ai_opt_out=ai_opt_out)


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """CC-124: every hop of a redirect is checked too, so a public page can't send us on to a private address."""
    def __init__(self, check):
        self.check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, headers: dict[str, str], check=None, timeout: float = TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US", **headers})
    if check is None:
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - http(s) only, checked by the caller
    check(url)
    return urllib.request.build_opener(_CheckedRedirects(check)).open(req, timeout=timeout)


def robots_for(url: str, check=None, timeout: float = TIMEOUT) -> Robots:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in _robots:
        try:
            with _open(origin + "/robots.txt", {}, check, timeout) as r:
                _robots[origin] = parse_robots(r.read(500_000).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise Blocked(f"the site refuses automated visitors (HTTP {e.code} for robots.txt)") from e
            _robots[origin] = Robots([], [], 0.0, False)  # no robots.txt: no rules
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(f"couldn't reach {parts.netloc}: {e}") from e
    return _robots[origin]


def fetch(url: str, etag: str = "", last_modified: str = "", ai: bool = True, check=None, timeout: float = TIMEOUT) -> Page:
    """Get a page, or a 304 when it hasn't changed. ai=True means its text will go to Claude, so an
    AI opt-out in robots.txt counts as a refusal. check (CC-124, a member's link) is called with each address
    asked for, redirects included, and raises Blocked for one we mustn't visit."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise Blocked(f"not a web address: {url}")
    rules = robots_for(url, check, timeout)
    if ai and rules.ai_opt_out:
        raise Blocked("the site's robots.txt opts out of AI crawlers, so its pages aren't sent to Claude")
    if not rules.allows(parts.path or "/"):
        raise Blocked("robots.txt asks crawlers not to read this page")
    gap = max(MIN_GAP_SECONDS, rules.crawl_delay)
    wait = _last_hit.get(parts.netloc, 0) + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        with _open(url, headers, check, timeout) as r:
            body = r.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise FetchError(f"page is larger than {MAX_BYTES // 1_000_000} MB")
            charset = r.headers.get_content_charset() or "utf-8"
            return Page(url=r.geturl(), status=r.status, text=body.decode(charset, "replace"),
                        etag=r.headers.get("ETag", ""), last_modified=r.headers.get("Last-Modified", ""),
                        content_type=r.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return Page(url=url, status=304, etag=etag, last_modified=last_modified)
        if e.code in (401, 403, 451):
            raise Blocked(f"the site refused the request (HTTP {e.code})") from e
        if e.code == 404:
            raise FetchError("page not found (HTTP 404): the address may have changed") from e
        raise FetchError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"couldn't reach {parts.netloc}: {e}") from e
    finally:
        _last_hit[parts.netloc] = time.monotonic()
