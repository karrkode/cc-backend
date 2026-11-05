"""A page's readable text, with each link's address after its text, ready for the model.

Scripts, styles, navigation, headers, footers and forms are dropped; whitespace is squeezed. The
same page gives the same text, so its hash tells whether anything changed since the last check.
"""
import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "template", "head", "button"}
BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "br", "dt", "dd", "td", "th"}
# About 15,000 tokens (CC-80): enough for a season or a month of shows; a longer page is cut off (and the
# result says so) rather than costing several times as much.
MAX_CHARS = 50_000
TAIL_CHARS = 10_000  # CC-222: of those, what's kept from the page's end when a footer matters


class _Text(HTMLParser):
    def __init__(self, base: str, drop: set[str]):
        super().__init__(convert_charrefs=True)
        self.base, self.drop, self.out, self.skip, self.href = base, drop, [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in self.drop:
            self.skip += 1
        elif tag in BLOCK:
            self.out.append("\n")
        if tag == "a" and not self.skip:
            href = dict(attrs).get("href") or ""
            self.href = urljoin(self.base, href) if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")) else None

    def handle_endtag(self, tag):
        if tag in self.drop and self.skip:
            self.skip -= 1
        elif tag == "a" and self.href and not self.skip:
            self.out.append(f" <{self.href}> "); self.href = None
        elif tag in BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def page_text(html: str, url: str, footers: bool = False) -> tuple[str, bool]:
    """(text, cut): the page's text and links; cut is True if it had to be shortened. CC-222: footers=True keeps headers
    and footers, and the end of a long page, since that's where a venue's site gives its address."""
    p = _Text(url, SKIP - {"footer", "header"} if footers else SKIP)
    p.feed(html); p.close()
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    text = re.sub(r" ?\n[ \n]*", "\n", text).strip()
    # drop links that point off the venue's own site (social media, ticketing ads) to save space
    host = urlsplit(url).netloc.removeprefix("www.")
    # CC-182: an address stops at the next "<" too, so each match ends where the next can start
    text = re.sub(r" ?<(https?://[^<>\s]+)>", lambda m: f" <{m.group(1)}>" if host in urlsplit(m.group(1)).netloc else "", text)
    if len(text) <= MAX_CHARS:
        return text, False
    return (text[:MAX_CHARS - TAIL_CHARS] + "\n" + text[-TAIL_CHARS:] if footers else text[:MAX_CHARS]), True


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
