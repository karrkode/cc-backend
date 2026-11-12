"""CC-204: photo links and visitor hashing have their own keys."""
import hashlib
import hmac
import time
from datetime import date

import pytest

from app import keys, photos, traffic
from app.config import get_settings


@pytest.fixture(autouse=True)
def fresh():
    keys.reset()
    yield
    keys.reset()


def old_sig(photo_id, exp, kind=b"went-photo-link:"):
    k = hashlib.sha256(kind + get_settings().jwt_secret.encode()).digest()
    return hmac.new(k, f"{photo_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def test_with_no_keys_set_nothing_changes(monkeypatch):
    s = get_settings()
    assert photos._sig(7, 123) == old_sig(7, 123) and photos._sig(7, 123, photos.OWN) == old_sig(7, 123, photos.OWN)
    old = hashlib.sha256(f"{s.jwt_secret}|vid|abc".encode()).hexdigest()[:40]
    assert traffic.visitor_key("abc", date(2026, 1, 1), "1.2.3.4", "ua") == (old, True)
    assert traffic.address_key(date(2026, 1, 1), "1.2.3.4") == hashlib.sha256(
        f"{s.jwt_secret}|address|2026-01-01|1.2.3.4".encode()).hexdigest()[:40]
    assert keys.describe() == {"photo_link": "jwt", "visitor_hash": "jwt"}


def test_own_keys_are_independent_of_the_sign_in_key(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "photo_link_secret", "p" * 40)
    monkeypatch.setattr(s, "visitor_hash_secret", "v" * 40)
    sig, visitor = photos._sig(7, 123), traffic.visitor_key("abc", date(2026, 1, 1), "a", "ua")
    assert sig != old_sig(7, 123)
    monkeypatch.setattr(s, "jwt_secret", "a-different-sign-in-key-of-32-chars!!")  # rotating it
    assert photos._sig(7, 123) == sig and traffic.visitor_key("abc", date(2026, 1, 1), "a", "ua") == visitor
    assert keys.describe() == {"photo_link": "env", "visitor_hash": "env"}


def test_photo_links_work_with_the_new_key_and_old_links_do_not(client, monkeypatch):
    from app.models import WentPhoto  # noqa: F401
    monkeypatch.setattr(get_settings(), "photo_link_secret", "p" * 40)
    exp = int(time.time()) + 600
    assert client.get(f"/photos/1?exp={exp}&sig={old_sig(1, exp)}").status_code == 404
    assert photos._sig(1, exp) != old_sig(1, exp)


def prod(monkeypatch, reads):
    s = get_settings()
    monkeypatch.setattr(s, "env", "prod")
    monkeypatch.setattr(keys, "_spawn", lambda fn, *a: fn(*a))  # the background refresh, run in place
    answers = iter(reads)

    def read(name):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(keys, "_read_param", read)
    return s


def test_prod_never_waits_for_parameter_store_and_falls_back(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(keys, "monotonic", lambda: clock[0])
    s = prod(monkeypatch, ["unset", "k" * 40])
    assert keys.photo_link_secret() == s.jwt_secret  # the first call starts the read, answers from the cache
    assert keys.photo_link_secret() == s.jwt_secret  # the read found the placeholder
    clock[0] += 601  # stale: starts another read, still answering from the cache
    assert keys.photo_link_secret() == s.jwt_secret
    assert keys.photo_link_secret() == "k" * 40
    assert keys.describe()["photo_link"] == "ssm"


def test_warm_reads_both_keys_at_startup(monkeypatch):
    prod(monkeypatch, ["a" * 40, "b" * 40])
    keys.warm()
    assert keys.photo_link_secret() == "a" * 40 and keys.visitor_hash_secret() == "b" * 40


def test_a_failed_read_keeps_the_last_good_value(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(keys, "monotonic", lambda: clock[0])
    prod(monkeypatch, ["p" * 40, "v" * 40, RuntimeError("ssm down"), "  short  "])
    keys.warm()
    assert keys.visitor_hash_secret() == "v" * 40
    clock[0] += 601
    assert keys.visitor_hash_secret() == "v" * 40  # stale: starts the read that fails
    assert keys.visitor_hash_secret() == "v" * 40  # still the last good value
    clock[0] += 61  # retried after a minute: the answer is too short to accept
    assert keys.visitor_hash_secret() == "v" * 40
    assert keys.visitor_hash_secret() == "v" * 40


def test_environment_value_wins_in_prod(monkeypatch):
    s = prod(monkeypatch, [])
    monkeypatch.setattr(s, "photo_link_secret", "e" * 40)
    assert keys.photo_link_secret() == "e" * 40  # never touches Parameter Store


def test_a_signed_link_survives_a_sign_in_key_change(monkeypatch):
    exp = int(time.time()) + 600
    monkeypatch.setattr(get_settings(), "photo_link_secret", "p" * 40)
    sig = photos._sig(5, exp)
    assert photos.link_ok(5, exp, sig)
    monkeypatch.setattr(get_settings(), "jwt_secret", "rotated-sign-in-key-of-32-characters!")
    assert photos.link_ok(5, exp, sig)  # rotating the sign-in key leaves photo links alone
    assert not photos.link_ok(5, exp, old_sig(5, exp))
