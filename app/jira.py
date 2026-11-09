"""CC-241: a narrow Jira Cloud adapter, so an admin can turn a member's report into a ticket in the CC project.

Only the server talks to Jira. It does two things: make a ticket, and find tickets by the label that names their
report (report-ISS-0007). That label is the durable link back to the report. It's also how a retry after a timeout
finds the ticket the first try made, instead of making a second.

What is sent is drafted from the report with personal details taken out (redact). The reporter's name and email
are never part of it.
"""
import base64
import json
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from time import monotonic

from .config import get_settings

log = logging.getLogger("uvicorn.error")  # uvicorn already prints this logger at INFO

ISSUE_TYPES = ("Bug", "Story", "Task")
PRIORITIES = ("Highest", "High", "Medium", "Low", "Lowest")
FEEDBACK_LABEL = "member-feedback"


class JiraError(Exception):
    """A failed Jira call, with what the admin should know. maybe_made: Jira may have made the ticket anyway
    (a timeout or an outage), so the claim is kept for a minute while Jira's search catches up."""

    def __init__(self, status: int, message: str, *, maybe_made: bool = False, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.message, self.maybe_made, self.retry_after = status, message, maybe_made, retry_after


@dataclass
class Ticket:
    key: str
    status: str = ""
    category: str = ""  # Jira's status category: new, indeterminate or done
    labels: tuple[str, ...] = ()


def report_label(report_key: str) -> str:
    return f"report-{report_key}"


def ticket_url(key: str) -> str:
    return f"{get_settings().jira_site.rstrip('/')}/browse/{key}"


# ---- keeping personal details out ---------------------------------------------------

_JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+")
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_URL_TAIL = re.compile(r"(https?://[^\s?#]+)[?#]\S*", re.I)  # query strings and #fragments (invite links carry tokens)
_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
_PHONE = re.compile(r"(?<![\w.])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w.])")


def redact(text: str, name: str = "") -> str:
    """Text with emails, phone numbers, long token-like strings and URL query strings and fragments taken out, and
    the reporter's name if it's given."""
    text = _JWT.sub("[token removed]", text or "")
    text = _EMAIL.sub("[email removed]", text)
    text = _URL_TAIL.sub(r"\1", text)
    text = _TOKEN.sub("[token removed]", text)
    text = _PHONE.sub("[phone removed]", text)
    name = (name or "").strip()
    if len(name) >= 3:
        text = re.sub(rf"\b{re.escape(name)}\b", "[name removed]", text, flags=re.I)
    return text


# ---- the two backends ---------------------------------------------------------------

def _adf(text: str) -> dict:
    """Plain text as Atlassian Document Format: a paragraph per blank-line block, line breaks kept."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        content: list[dict] = []
        for i, line in enumerate(block.split("\n")):
            if i:
                content.append({"type": "hardBreak"})
            if line:
                content.append({"type": "text", "text": line})
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


class JiraApi:
    """Jira Cloud's REST API v3, with an Atlassian account email and API token."""

    def __init__(self, credentials: str):
        self.auth = "Basic " + base64.b64encode(credentials.encode()).decode()
        s = get_settings()
        self.site, self.timeout = s.jira_site.rstrip("/"), s.jira_timeout_seconds

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.site + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": self.auth, "Accept": "application/json",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:  # noqa: S310 - our own Jira site, https
                return json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise _http_error(e.code, e.headers.get("Retry-After"), e.read()[:4000], method) from None
        except (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError) as e:
            raise JiraError(504, "Jira didn't answer in time.", maybe_made=method == "POST") from e

    def find(self, labels: list[str]) -> list[Ticket]:
        jql = f'project = {get_settings().jira_project} AND labels in ({", ".join(json.dumps(x) for x in labels)})'
        got = self._call("GET", "/rest/api/3/search/jql?" + urllib.parse.urlencode(
            {"jql": jql, "fields": "status,labels", "maxResults": 100}))
        return [Ticket(key=i["key"], status=i["fields"]["status"]["name"],
                       category=i["fields"]["status"].get("statusCategory", {}).get("key", ""),
                       labels=tuple(i["fields"].get("labels", []))) for i in got.get("issues", [])]

    def create(self, issue_type: str, priority: str, summary: str, description: str, labels: list[str]) -> str:
        got = self._call("POST", "/rest/api/3/issue", {"fields": {
            "project": {"key": get_settings().jira_project}, "issuetype": {"name": issue_type},
            "priority": {"name": priority}, "summary": summary, "description": _adf(description), "labels": labels}})
        return got["key"]


def _http_error(code: int, retry_after: str | None, body: bytes, method: str) -> JiraError:
    if code in (401, 403):
        return JiraError(502, f"Jira refused the server's credentials ({code}). The API token may have expired, or it "
                              "can't create work items in the CC project.")
    if code == 429:
        wait = int(retry_after) if (retry_after or "").isdigit() else 60
        return JiraError(503, f"Jira is limiting requests. Try again in {wait} seconds.", retry_after=wait)
    if code == 400:
        try:
            errs = json.loads(body)
            why = "; ".join([*errs.get("errorMessages", []), *(f"{k}: {v}" for k, v in errs.get("errors", {}).items())])
        except ValueError:
            why = ""
        return JiraError(422, "Jira didn't accept the ticket" + (f": {why}"[:300] if why else "."))
    if code == 404:
        return JiraError(502, "Jira couldn't find the CC project. Check the account can see it.")
    return JiraError(502, f"Jira had a problem ({code}).", maybe_made=method == "POST" and code >= 500)


class FakeJira:
    """An in-memory Jira, for tests and for trying the feature locally (JIRA_BACKEND=fake)."""

    def __init__(self):
        self.tickets: dict[str, dict] = {}
        self.next_number = 9001
        self.fail_next: JiraError | None = None  # tests: the next call fails like this
        self.made_then_fail: JiraError | None = None  # tests: the next create makes the ticket, then fails anyway
        self.calls: list[str] = []

    def _fail(self):
        if self.fail_next:
            err, self.fail_next = self.fail_next, None
            raise err

    def find(self, labels: list[str]) -> list[Ticket]:
        self.calls.append("find")
        self._fail()
        return [Ticket(key=k, status=t["status"], category=t["category"], labels=tuple(t["labels"]))
                for k, t in self.tickets.items() if set(t["labels"]) & set(labels)]

    def create(self, issue_type: str, priority: str, summary: str, description: str, labels: list[str]) -> str:
        self.calls.append("create")
        self._fail()
        key = f"{get_settings().jira_project}-{self.next_number}"
        self.next_number += 1
        self.tickets[key] = {"issue_type": issue_type, "priority": priority, "summary": summary, "description": description,
                             "labels": list(labels), "status": "Idea", "category": "new"}
        if self.made_then_fail:
            err, self.made_then_fail = self.made_then_fail, None
            raise err
        return key


FAKE = FakeJira()


@lru_cache(maxsize=1)
def _ssm_credentials(_: int) -> str:
    import boto3  # production only; the container's short-lived credentials can read this one parameter
    s = get_settings()
    got = boto3.session.Session().client("ssm", region_name=s.ssm_region).get_parameter(
        Name=s.jira_credentials_param, WithDecryption=True)
    return got["Parameter"]["Value"]


def client():
    """The Jira to use. Raises JiraError(503) when it isn't set up."""
    s = get_settings()
    if s.jira_mode == "fake":
        return FAKE
    if s.jira_mode != "api":
        raise JiraError(503, "Jira isn't connected here.")
    creds = s.jira_credentials
    if not creds:
        try:
            creds = _ssm_credentials(int(monotonic() // 600))  # read again every 10 minutes, so a new token is picked up
        except Exception:  # noqa: BLE001
            log.warning("jira: couldn't read the credentials parameter")
            creds = ""
    if ":" not in creds:  # unset, or still the placeholder
        raise JiraError(503, "Jira isn't connected yet: the server has no Jira API token.")
    return JiraApi(creds)
