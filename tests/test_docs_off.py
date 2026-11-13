"""CC-202: the interactive docs and the schema are off in production."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import docs_urls


def test_docs_are_off_in_prod_only():
    prod = TestClient(FastAPI(**docs_urls("prod")))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert prod.get(path).status_code == 404
    assert docs_urls("test") == {} and docs_urls("dev") == {}


def test_docs_stay_on_in_tests(client):
    assert client.get("/openapi.json").status_code == 200  # test_warm_reader needs it
