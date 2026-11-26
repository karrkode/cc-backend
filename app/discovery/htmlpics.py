"""CC-321: pictures that sit in a page's HTML but not in its preview tags. Pure parsing: no fetching, no JavaScript.

- listing_cards: the cards on a source's listing page, each with its picture, its links and its headings. A card belongs to
  an event when one of its links is the event's address or one of its headings is the event's title (card_for).
- shared_images: a picture that cards for different events carry (a site default, a sponsor, a banner) is nobody's.
- main_picture: the largest picture in an event page's main content.
- looks_missing: a page that says it isn't there (a "soft 404": the server answered 200).
Lazy-loaded pictures are read from the HTML as it is: data-src and friends, srcset, <picture><source>. Logos, icons, sprites,
tracking pixels, sponsor strips and the site's header, footer and navigation are never candidates.
"""
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
IMPLIED = {"li", "p", "dt", "dd", "tr", "td", "th", "option"}  # a new one closes the open one of its own kind
SKIPPED = {"script", "style", "noscript", "template", "svg"}
SITE_PARTS = {"header", "footer", "nav", "aside"}  # the site's own, unless inside the page's article or main
MAX_DEPTH = 120
MAX_CLIMB = 6
LAZY = ("data-src", "data-lazy-src", "data-original", "data-lazy", "data-url", "src")
LAZY_SET = ("data-srcset", "data-lazy-srcset", "srcset")
# class or id words of a block that is page furniture, not the event
FURNITURE = re.compile(r"(?:^|[\s_-])(?:nav|navbar|navigation|menu|footer|sponsors?|partners?|breadcrumbs?|cookies?|"
                       r"sidebar|related|recommended|newsletter|social)(?:$|[\s_-])", re.I)
# words in a picture's own address (as whole words of its path), or in its class, id or alt, that mark it as not the event's
_BAD = r"(?:logo|icon|sprite|pixel|spacer|tracking|favicon|badge|placeholder|avatar|sponsor|blank)s?"
BAD_ADDRESS = re.compile(rf"(?<![a-z0-9]){_BAD}(?![a-z0-9])|(?<![a-z0-9])1x1(?![a-z0-9])|\.svg$", re.I)
BAD_WORDS = re.compile(rf"(?<![a-z0-9]){_BAD}(?![a-z0-9])", re.I)
MIN_DECLARED = 200  # a width or height attribute under this is an icon (the fetched picture is checked again by size)
# a page's own "not found": at the start of its title or heading (a site name comes after, and a show called "Room 404" doesn't start so)
MISSING = re.compile(
    r"^\W*(?:(?:error\s*)?(?:404|410)\s*(?:$|[-–—|:.!]?\s*(?:page\s*)?(?:not found|error|missing)|[-–—|:.!]\s*$)"
    r"|(?:oops\W+|sorry\W+)?(?:the\s+|this\s+)?page\s+(?:you\W+\w+\W+looking for\s+)?(?:was\s+|is\s+|could\s+)?"
    r"(?:not found|n[o']t found|does ?n[o']?t exist|does not exist|can[’']?t be found|cannot be found|no longer (?:available|exists))"
    r"|not found\s*$)", re.I)


class _Node:
    __slots__ = ("tag", "attrs", "parent", "kids", "bits")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "_Node | None"):
        self.tag, self.attrs, self.parent, self.kids, self.bits = tag, attrs, parent, [], []

    def walk(self):
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed(n.kids))

    def text(self) -> str:
        parts = []
        for n in self.walk():
            parts.extend(n.bits)
        return re.sub(r"\s+", " ", " ".join(parts)).strip()


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur, self.depth, self.skip = self.root, 0, 0

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag in SKIPPED:
                self.skip += 1
            return
        if tag in SKIPPED:
            self.skip = 1
            return
        if tag in IMPLIED and self.cur.tag == tag and self.cur.parent is not None:
            self.cur, self.depth = self.cur.parent, self.depth - 1
        node = _Node(tag, {k.lower(): (v or "").strip() for k, v in attrs}, self.cur)
        self.cur.kids.append(node)
        if tag not in VOID and self.depth < MAX_DEPTH:
            self.cur, self.depth = node, self.depth + 1

    def handle_startendtag(self, tag, attrs):
        if tag in SKIPPED:  # <svg/> has no content and no end tag to wait for
            return
        self.handle_starttag(tag, attrs)
        if tag not in VOID and tag not in SKIPPED:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag in SKIPPED:
                self.skip -= 1
            return
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            up = 0
            c = self.cur
            while c is not n:
                c, up = c.parent, up + 1
            self.cur, self.depth = n.parent, max(0, self.depth - up - 1)

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.cur.bits.append(data.strip())


def _tree(html: str) -> _Node:
    p = _Tree()
    try:
        p.feed(html or "")
        p.close()
    except Exception:  # noqa: BLE001 - broken markup: use what was read
        pass
    return p.root


def _absolute(url: str, base: str) -> str | None:
    url = (url or "").strip()
    if not url or url.startswith(("data:", "blob:", "javascript:")):
        return None
    try:
        full = urljoin(base, url)
        parts = urlsplit(full)
    except ValueError:
        return None
    return full if parts.scheme in ("http", "https") and parts.hostname else None


def _srcset(value: str) -> list[tuple[float, str]]:
    """(size, address) from a srcset: the size is the w descriptor, or x times 1000, or 0 when there is none."""
    out = []
    for piece in re.split(r",\s+|,$", (value or "").strip()):
        bits = piece.split()
        if not bits:
            continue
        size = 0.0
        if len(bits) > 1:
            m = re.fullmatch(r"([\d.]+)([wx])", bits[1].lower())
            if m:
                size = float(m[1]) * (1000 if m[2] == "x" else 1)
        out.append((size, bits[0]))
    return out


def _number(v: str) -> int:
    m = re.match(r"\d+", v or "")
    return int(m[0]) if m else 0


def _within(node: _Node) -> bool:
    """Whether the node sits in page furniture. A <header>, <footer>, <nav> or <aside> inside the page's own <article> or
    <main> belongs to that article, so only the site's own count."""
    inside_main = False
    n = node.parent
    while n is not None and n.tag not in ("body", "html"):  # a body class such as "no-sidebar" describes the page, not a block
        if n.tag in ("main", "article"):
            inside_main = True
        if n.tag in SITE_PARTS and not (inside_main and n.tag in ("header", "footer", "aside")):
            return True
        if FURNITURE.search(f"{n.attrs.get('class', '')} {n.attrs.get('id', '')}"):
            return True
        n = n.parent
    return False


@dataclass
class _Img:
    node: _Node
    urls: list[str]  # best first, as full addresses
    area: int


def _images(root: _Node, base: str) -> list[_Img]:
    """Every usable <img> in page order, with its candidate addresses (largest srcset entry first)."""
    out = []
    for n in root.walk():
        if n.tag != "img":
            continue
        a = n.attrs
        w, h = _number(a.get("width", "")), _number(a.get("height", ""))
        if (w and w < MIN_DECLARED) or (h and h < MIN_DECLARED and not w) or (w and h and min(w, h) < MIN_DECLARED):
            continue
        sets = [e for k in LAZY_SET for e in _srcset(a.get(k, ""))]
        if n.parent is not None and n.parent.tag == "picture":
            for s in n.parent.kids:
                if s.tag == "source":
                    sets += [e for k in LAZY_SET for e in _srcset(s.attrs.get(k, ""))]
        sets.sort(key=lambda e: -e[0])
        raw = [u for _, u in sets] + [a[k] for k in LAZY if a.get(k)]
        urls = []
        for u in raw:
            full = _absolute(u, base)
            if full and full not in urls:
                urls.append(full)
        path = urlsplit(urls[0]).path if urls else ""
        if not urls or BAD_ADDRESS.search(path) or BAD_WORDS.search(f"{a.get('class', '')} {a.get('id', '')} {a.get('alt', '')}"):
            continue
        if _within(n):
            continue
        widest = max([w] + [int(s) for s, _ in sets if s and s < 100000])
        out.append(_Img(n, urls, int(widest * (h or widest * 0.6))))  # an unknown height counts as 3:5
    return out


# --- cards on a listing page -------------------------------------------------------------------------------------------

def _norm(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text or "").split())


def _same_address(a: str, b: str) -> bool:
    def key(u: str) -> tuple:
        p = urlsplit(u.split("#")[0])
        return (p.scheme.lower(), (p.hostname or "").lower(), p.path.rstrip("/"), p.query)
    try:
        return key(a) == key(b)
    except ValueError:
        return False


@dataclass
class Card:
    image: str  # the card's picture, as a full address
    links: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)

    @property
    def who(self) -> str:
        """What the card is for, to tell cards apart: its first heading, else its first link, else the picture."""
        return _norm(self.headings[0]) if self.headings else (self.links[0] if self.links else self.image)


def _headings(node: _Node) -> list[str]:
    return [t for n in node.walk() if n.tag in ("h1", "h2", "h3", "h4", "h5", "h6") and (t := n.text())]


def listing_cards(html: str, base: str) -> list[Card]:
    """The picture cards on a listing page. A card is the largest block around one picture that holds no other picture and
    no other heading: its links and headings say which event it is."""
    root = _tree(html)
    images = _images(root, base)
    owner = {id(i.node): i for i in images}
    cards = []
    for img in images:
        node = img.node
        for _ in range(MAX_CLIMB):
            up = node.parent
            if up is None or up.tag in ("body", "html", "#root", "main", "ul", "ol", "table", "tbody", "section"):
                break
            if sum(1 for n in up.walk() if id(n) in owner) > 1 or len({_norm(h) for h in _headings(up)}) > 1:
                break
            node = up
        links = []
        for n in node.walk():
            if n.tag == "a" and n.attrs.get("href"):
                full = _absolute(n.attrs["href"], base)
                if full and full not in links:
                    links.append(full)
        # a picture that is itself a link's content counts that link, even if the block climbed no further
        p = img.node.parent
        while p is not None and p is not node.parent:
            if p.tag == "a" and p.attrs.get("href"):
                full = _absolute(p.attrs["href"], base)
                if full and full not in links:
                    links.append(full)
            p = p.parent
        cards.append(Card(img.urls[0], links, _headings(node)))
    return cards


def shared_images(cards: list[Card]) -> set[str]:
    """Pictures that cards for two or more different events carry: refused for all of them."""
    who: dict[str, set[str]] = {}
    for c in cards:
        who.setdefault(c.image, set()).add(c.who)
    return {image for image, names in who.items() if len(names) > 1}


def card_for(cards: list[Card], title: str, url: str = "") -> Card | None:
    """The card whose link is the event's address, else the one whose heading is the event's title. Never a guess."""
    if url:
        for c in cards:
            if any(_same_address(link, url) for link in c.links):
                return c
    want = _norm(title)
    if want:
        for c in cards:
            if any(_norm(h) == want for h in c.headings):
                return c
    return None


# --- an event's own page -----------------------------------------------------------------------------------------------

def main_picture(html: str, base: str) -> list[str]:
    """The candidate addresses of the largest picture in the page's main content, best first. Inside <main> or <article>
    when the page has one, else anywhere that isn't furniture. When no size is declared, the first in the page wins."""
    root = _tree(html)
    images = _images(root, base)
    if not images:
        return []

    def in_main(i: _Img) -> bool:
        n = i.node.parent
        while n is not None:
            if n.tag in ("main", "article"):
                return True
            n = n.parent
        return False

    pool = [i for i in images if in_main(i)] or images
    best = max(range(len(pool)), key=lambda k: (pool[k].area, -k))
    return pool[best].urls


def looks_missing(html: str) -> bool:
    """A page that gives its own 404 with a 200: its title or first heading says so."""
    root = _tree(html)
    for n in root.walk():
        if n.tag in ("title", "h1") and MISSING.search(n.text()):
            return True
    return False
