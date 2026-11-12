"""CC-204: one key per purpose. Photo links and visitor hashing each have their own secret, so rotating the
sign-in key (or either of these) doesn't touch the others.

Order: the environment setting, then (prod only) the Parameter Store value, then the old derivation from
jwt_secret. That last step keeps links and hashes exactly as they were until the parameter holds a real value,
so a deploy before `terraform apply` changes nothing.
"""
import logging
import threading
from time import monotonic

from .config import get_settings

log = logging.getLogger("keys")

_REFRESH_SECONDS = 600
_RETRY_SECONDS = 60
_lock = threading.Lock()
_cache: dict[str, dict] = {}  # param name -> {"value": str | None, "next": float}


def reset() -> None:
    with _lock:
        _cache.clear()


def _read_param(name: str) -> str:
    import boto3  # production only; the container's short-lived credentials may read these parameters
    from botocore.config import Config
    s = get_settings()
    cfg = Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 0})
    got = boto3.session.Session().client("ssm", region_name=s.ssm_region, config=cfg).get_parameter(
        Name=name, WithDecryption=True)
    return got["Parameter"]["Value"]


def _refresh(name: str) -> None:
    """Read the parameter (no lock held during the network call), then store the result."""
    with _lock:
        value = _cache.get(name, {}).get("value")
    wait = _REFRESH_SECONDS
    try:
        got = _read_param(name).strip()
        if len(got) >= 32 and got != "unset":
            value = got
        elif value is None:
            log.warning("keys: a purpose key isn't set yet, using the derived key")
    except Exception:  # noqa: BLE001
        log.warning("keys: couldn't read a purpose key from Parameter Store")
        wait = _RETRY_SECONDS
    with _lock:
        _cache[name] = {"value": value, "next": monotonic() + wait, "busy": False}


def _spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _from_ssm(name: str) -> str | None:
    """The cached value, never waiting on the network (this runs on the event loop). A stale or missing entry
    starts a background refresh and the old value (or None) is used meanwhile. A failed read keeps the last good
    value and retries in a minute. warm() fills the cache at startup."""
    with _lock:
        entry = _cache.setdefault(name, {"value": None, "next": 0.0, "busy": False})
        if monotonic() < entry["next"] or entry["busy"]:
            return entry["value"]
        entry["busy"] = True
        value = entry["value"]
    _spawn(_refresh, name)
    return value


def warm() -> None:
    """Read both parameters once at startup (prod only), so the first requests already have their keys."""
    s = get_settings()
    if s.env == "prod":
        for name in (s.photo_link_secret_param, s.visitor_hash_secret_param):
            _refresh(name)


def _resolve(env_value: str, param: str) -> tuple[str, str]:
    if env_value:
        return env_value, "env"
    s = get_settings()
    if s.env == "prod":
        got = _from_ssm(param)
        if got:
            return got, "ssm"
    return s.jwt_secret, "jwt"


def photo_link_secret() -> str:
    s = get_settings()
    return _resolve(s.photo_link_secret, s.photo_link_secret_param)[0]


def visitor_hash_secret() -> str:
    s = get_settings()
    return _resolve(s.visitor_hash_secret, s.visitor_hash_secret_param)[0]


def describe() -> dict[str, str]:
    """Where each key comes from (env, ssm or jwt), never the keys themselves. For checking production."""
    s = get_settings()
    return {"photo_link": _resolve(s.photo_link_secret, s.photo_link_secret_param)[1],
            "visitor_hash": _resolve(s.visitor_hash_secret, s.visitor_hash_secret_param)[1]}
