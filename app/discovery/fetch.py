"""Fetching venues' pages politely.

- Every request says who we are (USER_AGENT) and has a timeout and a size cap.
- robots.txt is followed for our user agent, and a site that disallows AI crawlers (ClaudeBot,
  anthropic-ai and the like) is treated as off limits too: its pages would be read by Claude.
- Pages are fetched with If-None-Match / If-Modified-Since, so an unchanged page usually costs a
  tiny 304 reply and nothing else.
- Requests to one site are spaced out (MIN_GAP_SECONDS, or the site's own Crawl-delay).
- CC-189: only public web hosts are reached, whoever asked for the page: every request and redirect is checked
  here, and the connection goes to the address that was checked (see _connect_public).
There is no logging in, no pretending to be a browser and no retrying around a refusal: a refused
page is reported as blocked, for a person to check by hand.
"""
import codecs
import http.client
import ipaddress
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

USER_AGENT = "CultureCrewBot/1.0 (+https://chiculturecrew.com; info@chiculturecrew.com)"
AI_AGENTS = {"claudebot", "anthropic-ai", "claude-web", "claude-searchbot"}
TIMEOUT = 25
MAX_BYTES = 6_000_000
MIN_GAP_SECONDS = 3
# CC-182: the charsets a page may name; any other is read as utf-8
CHARSETS = {"utf-8", "iso8859-1", "cp1252", "ascii"}
CHUNK = 64_000
# CC-189: ranges that can lead to an IPv4 host or aren't web hosts, listed here because is_global lets some through
NOT_PUBLIC = [ipaddress.ip_network(n) for n in (
    "::/96", "::ffff:0:0:0/96",  # IPv4-compatible and IPv4-translated
    "64:ff9b::/96", "64:ff9b:1::/48",  # NAT64
    "2001::/32", "2002::/16", "192.88.99.0/24",  # Teredo, 6to4 and 6to4's relays
)]

_robots: dict[str, "Robots"] = {}
_last_hit: dict[str, float] = {}


class Blocked(Exception):
    """The site doesn't allow us (robots.txt, an AI opt-out, or a refusal such as 403)."""


class FetchError(Exception):
    """Couldn't get the page this time (network, timeout, 5xx). Worth trying again later."""


class PrivateAddress(Blocked):
    """The address (or a redirect) points somewhere that isn't a public web host."""


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
    unsure: bool = False  # CC-216: the site didn't answer clearly, so these empty rules hold for this read only

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


def _addresses(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    ip = getattr(ip, "ipv4_mapped", None) or ip
    # is_global is False for loopback, private, link-local (169.254.169.254), carrier-grade NAT, reserved...
    return ip.is_global and not ip.is_multicast and not any(ip in net for net in NOT_PUBLIC)


def public_addresses(host: str, port: int) -> list[str]:
    """host's addresses, or PrivateAddress unless every one of them is public."""
    try:
        found = _addresses(host, port)
    except (socket.gaierror, UnicodeError) as e:
        raise FetchError(f"there's no website at {host}") from e
    if not all(is_public(a) for a in found):
        raise PrivateAddress("that link points to a private or local address")
    return found


def _check_form(url: str) -> tuple[str, int]:
    """The host and port of an http(s) address on the usual port, or PrivateAddress for anything else."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise PrivateAddress("only http and https links to public websites can be read")
    try:
        port = parts.port
    except ValueError as e:
        raise PrivateAddress("that link has a port we can't read") from e
    if port not in (None, 80, 443):
        raise PrivateAddress("only links on the usual web ports can be read")
    return parts.hostname, port or (443 if parts.scheme == "https" else 80)


def check_public(url: str) -> None:
    """Raise PrivateAddress unless url is http(s) on the usual port, at a host whose every address is public. An
    early answer for a member's link: what protects a fetch is the check when connecting."""
    public_addresses(*_check_form(url))


def _connect_public(address: tuple[str, int], timeout, source_address=None) -> socket.socket:
    """CC-189: looks the host up once, checks the answer and connects to that same answer, so a second, different
    DNS reply can't be used. http.client keeps the hostname for the Host header, SNI and the certificate check."""
    host, port = address
    failed = OSError(f"no addresses for {host}")
    for ip in public_addresses(host, port):
        try:
            return socket.create_connection((ip, port), timeout, source_address)
        except OSError as e:
            failed = e
    raise failed


class _Public:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public  # how http.client opens its socket, for http and https alike


class _PublicHTTP(_Public, http.client.HTTPConnection):
    pass


class _PublicHTTPS(_Public, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTP, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPS, req, context=self._context)


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """CC-124: every hop of a redirect is checked too, so a public page can't send us on to a private address."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_form(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@lru_cache(maxsize=1)
def _opener() -> urllib.request.OpenerDirector:
    """CC-189: only what's needed for http(s), so no proxy setting or other kind of address gets around the check."""
    opener = urllib.request.OpenerDirector()
    for handler in (_HTTPHandler(), _HTTPSHandler(), _CheckedRedirects(), urllib.request.HTTPErrorProcessor(),
                    urllib.request.HTTPDefaultErrorHandler()):
        opener.add_handler(handler)
    return opener


def _open(url: str, headers: dict[str, str], timeout: float = TIMEOUT):
    _check_form(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US", **headers})
    return _opener().open(req, timeout=timeout)


def robots_for(url: str, timeout: float = TIMEOUT) -> Robots:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in _robots:
        try:
            with _open(origin + "/robots.txt", {}, timeout) as r:
                _robots[origin] = parse_robots(r.read(500_000).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise Blocked(f"the site refuses automated visitors (HTTP {e.code} for robots.txt)") from e
            _robots[origin] = Robots([], [], 0.0, False, unsure=e.code not in (404, 410))  # no robots.txt: no rules
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(f"couldn't reach {parts.netloc}: {e}") from e
    return _robots[origin]


def charset_of(name: str | None) -> str:
    """CC-182: the codec for a page's named charset, if it's one we read; else utf-8."""
    try:
        codec = codecs.lookup(name or "utf-8").name
    except (LookupError, TypeError, ValueError):
        return "utf-8"
    return codec if codec in CHARSETS else "utf-8"


def _read(r, max_bytes: int, deadline: float | None) -> bytes:
    """The body, up to max_bytes (FetchError beyond it), stopping at deadline (a time.monotonic() value)."""
    parts, size = [], 0
    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise FetchError("the page took too long to load")
        chunk = r.read1(min(CHUNK, max_bytes + 1 - size))  # what's arrived, not a full chunk, so the deadline is seen
        if not chunk:
            return b"".join(parts)
        parts.append(chunk); size += len(chunk)
        if size > max_bytes:
            raise FetchError(f"page is larger than {max_bytes / 1_000_000:g} MB")


def _polite(url: str, ai: bool, timeout: float, wait: bool) -> None:
    """robots.txt (and, when ai, its AI opt-out), then the gap between requests to one site."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise Blocked(f"not a web address: {url}")
    rules = robots_for(url, timeout)
    if ai and rules.ai_opt_out:
        raise Blocked("the site's robots.txt opts out of AI crawlers, so its pages aren't sent to Claude")
    if not rules.allows(parts.path or "/"):
        raise Blocked("robots.txt asks crawlers not to read this page")
    gap = max(MIN_GAP_SECONDS, rules.crawl_delay)
    pause = _last_hit.get(parts.netloc, 0) + gap - time.monotonic()
    if wait and pause > 0:
        time.sleep(pause)


def _refused(e: urllib.error.HTTPError) -> Exception:
    if e.code in (401, 403, 451):
        return Blocked(f"the site refused the request (HTTP {e.code})")
    if e.code == 404:
        return FetchError("page not found (HTTP 404): the address may have changed")
    return FetchError(f"HTTP {e.code}")


def fetch(url: str, etag: str = "", last_modified: str = "", ai: bool = True, timeout: float = TIMEOUT,
          deadline: float | None = None, max_bytes: int = MAX_BYTES, wait: bool = True) -> Page:
    """Get a page, or a 304 when it hasn't changed. ai=True means its text will go to Claude, so an
    AI opt-out in robots.txt counts as a refusal. CC-182: deadline (a time.monotonic() value) bounds the whole
    download, not just each read; wait=False skips the gap between requests to one site, for a single page a
    member is waiting on."""
    parts = urlsplit(url)
    _polite(url, ai, timeout, wait)
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        with _open(url, headers, timeout) as r:
            body = _read(r, max_bytes, deadline)
            return Page(url=r.geturl(), status=r.status, text=body.decode(charset_of(r.headers.get_content_charset()), "replace"),
                        etag=r.headers.get("ETag", ""), last_modified=r.headers.get("Last-Modified", ""),
                        content_type=r.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return Page(url=url, status=304, etag=etag, last_modified=last_modified)
        raise _refused(e) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"couldn't reach {parts.netloc}: {e}") from e
    finally:
        _last_hit[parts.netloc] = time.monotonic()


@dataclass
class File:
    url: str
    data: bytes
    content_type: str


def fetch_file(url: str, accept: str, max_bytes: int, timeout: float = TIMEOUT) -> File:
    """CC-246: a file (an event's picture) by the same rules as a page: public hosts only, robots.txt, the gap, a
    size cap and a deadline. Nothing fetched this way goes to Claude, so an AI opt-out doesn't apply."""
    parts = urlsplit(url)
    _polite(url, False, timeout, True)
    try:
        with _open(url, {"Accept": accept}, timeout) as r:
            data = _read(r, max_bytes, time.monotonic() + timeout * 2)
            return File(url=r.geturl(), data=data, content_type=r.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as e:
        raise _refused(e) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"couldn't reach {parts.netloc}: {e}") from e
    finally:
        _last_hit[parts.netloc] = time.monotonic()
