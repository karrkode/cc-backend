"""CC-302: finding a show's exact dates on the tickets or calendar page its own page links to.

A list page's weekly pattern is wrong around holidays and irregular runs, but the exact dates are often on a tickets page
(the theater's own, or a box office's) that nothing read. For a found or live performance still without dates:
1. links_of: every link on the event's page, off-site ones too (page_text drops those), kept to ones that look like tickets
   or a calendar by their text or path, so a page with none costs nothing.
2. Claude Haiku picks one by number (readers.read_link_choice). The address always comes from the page's own links, never
   from the model's reply, and everything it reads is data in the user message (CC-198).
3. The page is fetched with fetch.fetch, so every polite rule holds: public addresses only (checked again on each redirect),
   robots.txt and its AI-crawler opt-out, spacing between requests, no browser emulation (CC-189). A refused page is
   noted on the item and not asked again.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

MAX_LINKS = 30  # shown to the model, at most
MAX_REFUSED = 300  # one refusal line in the item's note
MAX_LINK = 200  # a longer address is skipped, so a refusal line always holds it whole
TICKET_WORDS = ("ticket", "calendar", "buy", "book", "schedule", "dates", "showtimes", "performances", "box office",
                "box-office", "on sale", "seats", "events", "whats-on", "what's on")
SKIP_SUFFIX = (".pdf", ".ics", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".mp3", ".mp4")


class _Anchors(HTMLParser):
    """Every link with its text, whatever it's inside: ticket buttons often sit in a header or nav."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href, self.text = None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href, self.text = dict(attrs).get("href") or "", []

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append((self.href, re.sub(r"\s+", " ", " ".join(self.text)).strip()))
            self.href = None


def links_of(html: str, base: str, any_link: bool = False, limit: int = MAX_LINKS) -> list[tuple[str, str]]:
    """(address, text) of the links on a page that might list a show's dates: web addresses other than the page itself, once
    each, in page order, at most `limit`. CC-304: any_link keeps links whatever their words, for the stronger model to choose from."""
    parser = _Anchors()
    parser.feed(html)
    parser.close()
    here, seen, out = base.split("#")[0].rstrip("/"), set(), []
    for href, text in parser.links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "sms:")):
            continue
        try:
            url = urljoin(base, href).split("#")[0]
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or url.rstrip("/") == here or url in seen:
            continue
        if len(url) > MAX_LINK or parts.path.lower().endswith(SKIP_SUFFIX):
            continue
        hint = f"{text} {parts.path}".lower()
        if not any_link and not any(w in hint for w in TICKET_WORDS):
            continue
        seen.add(url)
        out.append((url, text[:80]))
        if len(out) >= limit:
            break
    return out
