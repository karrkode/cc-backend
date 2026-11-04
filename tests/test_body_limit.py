"""CC-190: oversized requests are refused before they're read; the photo upload keeps its own, higher limit."""
import asyncio

import pytest

from app import photos
from app.bodylimit import MAX_BYTES, BodyLimit

from .conftest import auth
from .test_went_photos import JPEG, been, stored, upload  # noqa: F401 (stored is a fixture)

LOGIN = {"email": "ana@example.com", "password": "correct-horse-1"}


def stream(*chunks):
    """A body with no Content-Length, as chunked uploads have."""
    yield from chunks


def test_a_declared_size_over_the_limit_is_refused(client):
    r = client.post("/auth/login", content=b"x" * (MAX_BYTES + 1), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["detail"]
    assert r.headers["connection"] == "close"


def test_a_body_at_the_limit_reaches_the_route(client):
    r = client.post("/auth/login", content=b"x" * MAX_BYTES, headers={"Content-Type": "application/json"})
    assert r.status_code == 422  # not JSON, but it got that far


@pytest.mark.parametrize("path", ["/auth/login", "/traffic"])
def test_a_streamed_body_over_the_limit_is_refused(client, path):
    r = client.post(path, content=stream(b"x" * (MAX_BYTES + 1)), headers={"Content-Type": "application/json"})
    assert "content-length" not in r.request.headers
    assert r.status_code == 413 and r.json()["detail"]


def test_a_refused_body_can_be_read_by_the_site(client):
    r = client.post("/auth/login", content=b"x" * (MAX_BYTES + 1),
                    headers={"Content-Type": "application/json", "Origin": "http://localhost:4173"})
    assert r.status_code == 413 and r.headers["access-control-allow-origin"] == "http://localhost:4173"


def test_reading_stops_once_the_limit_is_passed():
    """Run the middleware alone, on a body that would never end: nothing past the limit is asked for."""
    asked = 0

    async def receive():
        nonlocal asked
        asked += 1
        return {"type": "http.request", "body": b"x" * 400_000, "more_body": True}

    async def app(scope, receive, send):
        while (await receive())["more_body"]:
            pass

    async def run():
        await BodyLimit(app)({"type": "http", "method": "POST", "path": "/traffic", "headers": []}, receive, None)

    with pytest.raises(Exception) as refused:
        asyncio.run(run())
    assert refused.value.status_code == 413 and asked == 3


def test_a_size_that_cant_be_read_falls_back_on_counting():
    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}
    got = []

    async def app(scope, receive, send):
        got.append(await receive())

    scope = {"type": "http", "method": "POST", "path": "/traffic", "headers": [(b"content-length", b"9" * 5000)]}
    asyncio.run(BodyLimit(app)(scope, receive, None))
    scope["headers"] = [(b"content-length", b"lots")]
    asyncio.run(BodyLimit(app)(scope, receive, None))
    assert len(got) == 2


def test_a_photo_may_be_bigger_than_a_json_body(client, user_token, make_event, stored):
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    assert upload(client, user_token, ev.id, JPEG + b"0" * (photos.MAX_BYTES - len(JPEG))).status_code == 200


def test_a_photo_over_its_own_limit_is_refused(client, user_token, make_event, stored):
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    too_big = JPEG + b"0" * photos.MAX_BYTES
    assert "too big" in upload(client, user_token, ev.id, too_big).json()["detail"]
    streamed = client.put(f"/me/attended/{ev.id}/photo", content=stream(too_big),
                          headers={**auth(user_token), "Content-Type": "image/jpeg"})
    assert streamed.status_code == 413 and "too big" in streamed.json()["detail"]


def test_only_the_upload_itself_gets_the_photo_limit(client, user_token):
    big = b"x" * (MAX_BYTES + 1)
    assert client.post("/me/attended/1/photo", content=big, headers=auth(user_token)).status_code == 413
    assert client.put("/me/attended/1/photo/extra", content=big, headers=auth(user_token)).status_code == 413
