"""CC-182: work for fromlink.bounded_read's own process in tests. It lives in a module of its own, which the
process imports by name."""
import os
import time

from app.discovery.jsonld import events_in, page_titles, visible_title
from app.discovery.pagetext import page_text


def spin(url):
    while True:
        pass


def sleep(url):
    time.sleep(3600)


def die(url):
    os._exit(1)


def echo(url):
    return "ok", {"url": url}


def hostile(url):
    """Read pages shaped to be slow for a backtracking reader, each the largest a member's link may download."""
    size, started = 3_000_000, time.monotonic()
    for page in ("<script " * (size // 8), "<title>" * (size // 7), "<meta " * (size // 6) + ">",
                 '<script type="application/ld+json">' * (size // 36)):
        events_in(page, url, ""); visible_title("A show", page); page_titles(page)
    page_text("&lt;http://x" * (size // 12), url)
    return "ok", {"seconds": time.monotonic() - started}
