"""CC-287: the headers on the HTML pages the API renders itself (/e/, /chicago/): the CSP and frame protection.

CloudFront already sends these for chiculturecrew.com (CC-193, infra/aws/frontend.tf). The API host serves the same pages
directly, and that host is the one the sign-in cookie belongs to, so every text/html response carries them here too.
PAGE_CSP is the same policy as `csp_api_pages` in frontend.tf (tests/test_csp.py keeps the two equal).

Behind CloudFront nothing doubles up: its response headers policy has override = true, so its copy replaces this one
(one header, not two). Caddy only adds a CSP to application/json answers. Locally, web.nginx.conf hides this one too.
"""
import base64
import hashlib

from .seo import THEME_SCRIPT

THEME_HASH = "sha256-" + base64.b64encode(hashlib.sha256(THEME_SCRIPT.encode()).digest()).decode()

PAGE_CSP = "; ".join([
    "default-src 'none'",
    f"script-src '{THEME_HASH}'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'none'",
])

HEADERS = ((b"content-security-policy", PAGE_CSP.encode()), (b"x-frame-options", b"DENY"))

# Swagger and ReDoc (off in production, CC-202) load their scripts from a CDN, so the full policy would blank them.
# They still refuse framing.
DOCS_PATHS = ("/docs", "/redoc")
DOCS_HEADERS = ((b"content-security-policy", b"frame-ancestors 'none'"), (b"x-frame-options", b"DENY"))


class PageHeaders:
    """ASGI middleware: adds the headers to every text/html response, unless the route already set its own.
    An HTML route must not set its own CSP: it would be kept even if weaker than PAGE_CSP."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        added = DOCS_HEADERS if scope["path"] in DOCS_PATHS else HEADERS

        async def with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                is_html = any(k.lower() == b"content-type" and v.lower().startswith(b"text/html") for k, v in headers)
                if is_html:
                    headers += [(k, v) for k, v in added if k not in names]
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)
