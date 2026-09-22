"""
API-boundary classification: interactive vs deferrable.

Interactive calls fail fast (502) during an outage; deferrable calls are queued
(202) so they survive it. Uses FastAPI's TestClient (which runs the lifespan, so
the background worker starts and stops cleanly).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app, router

HEADERS = {"X-Tenant-Id": "acme", "X-Feature": "demo"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def _set_outage(down: bool) -> None:
    for provider in router.pool:
        provider.healthy = not down


def test_interactive_succeeds_when_healthy():
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions", json=BODY, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["served_by"] is not None


def test_interactive_fails_fast_on_outage():
    _set_outage(True)
    try:
        with TestClient(app) as client:
            r = client.post("/v1/chat/completions", json=BODY, headers=HEADERS)
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "all_providers_failed"
    finally:
        _set_outage(False)


def test_deferrable_is_queued_on_outage():
    _set_outage(True)
    try:
        with TestClient(app) as client:
            r = client.post(
                "/v1/chat/completions",
                json=BODY,
                headers={**HEADERS, "X-Request-Mode": "deferrable"},
            )
            assert r.status_code == 202
            body = r.json()
            assert body["job_id"].startswith("job-")
            assert body["status"] in ("queued", "running")

            # The job is pollable.
            jr = client.get(body["status_url"])
            assert jr.status_code == 200
            assert jr.json()["job_id"] == body["job_id"]
    finally:
        _set_outage(False)


def test_deferrable_served_inline_when_healthy():
    # When providers are up, a deferrable request is served immediately (200),
    # not queued.
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json=BODY,
            headers={**HEADERS, "X-Request-Mode": "deferrable"},
        )
    assert r.status_code == 200


def test_unknown_job_returns_404():
    with TestClient(app) as client:
        r = client.get("/v1/jobs/job-does-not-exist")
    assert r.status_code == 404
