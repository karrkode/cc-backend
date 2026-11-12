"""CC-193: the Content-Security-Policy in infra/aws/frontend.tf and web.nginx.conf matches the pages the API renders."""
import base64
import hashlib
import re
from pathlib import Path

from app import seo

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "infra/aws/frontend.tf").read_text()
NGINX = (ROOT / "web.nginx.conf").read_text()
LOCAL_SOURCES = " http://localhost:* http://127.0.0.1:*"


def _hash() -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(seo.THEME_SCRIPT.encode()).digest()).decode()


def _tf_policy(name: str) -> str:
    block = re.search(rf"{name} = join\(\"; \", \[(.*?)\n  \]\)", TF, re.S).group(1)
    parts = re.findall(r'^\s*"(.*)",$', block, re.M)
    return "; ".join(p.replace("${local.csp_theme_hash}", _hash()) for p in parts)


def _nginx_policies() -> list[str]:
    return re.findall(r'add_header Content-Security-Policy "([^"]*)"', NGINX)


def test_theme_script_hash_is_in_terraform():
    assert f'csp_theme_hash = "{_hash()}"' in TF


def test_nginx_policies_equal_terraform_apart_from_local_sources():
    site, api = _nginx_policies()
    assert site.replace(LOCAL_SOURCES, "") == _tf_policy("csp_site")
    assert api == _tf_policy("csp_api_pages")


def test_no_script_source_allows_inline_or_eval():
    for policy in (_tf_policy("csp_site"), _tf_policy("csp_api_pages")):
        script = re.search(r"script-src ([^;]*)", policy).group(1)
        assert "unsafe-inline" not in script and "unsafe-eval" not in script


def _executable_inline_scripts(html: str) -> list[str]:
    # JSON-LD and other data blocks never run, so the policy does not govern them
    return [m.group(2) for m in re.finditer(r"<script([^>]*)>(.*?)</script>", html, re.S)
            if "ld+json" not in m.group(1)]


def test_every_rendered_page_has_only_the_hashed_inline_script(client, make_event):
    ev = make_event(title="Hamlet")
    paths = [f"/e/{ev.id}-hamlet", "/e/999999-nothing", "/chicago/", "/chicago/no-such-roundup", "/chicago/this-weekend"]
    for path in paths:
        html = client.get(path).text
        assert _executable_inline_scripts(html) == [seo.THEME_SCRIPT], path
        assert not re.search(r"\son\w+=", html) and "<script src" not in html, path
        assert "<img" not in html.replace('<img src="data:', ""), path  # img-src allows 'self' and data: only
