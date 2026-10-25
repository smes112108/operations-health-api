from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3

from fastapi.testclient import TestClient
import pytest

from app.main import create_app


def event(key="camera-demo-1", **changes):
    return {"event_key": key, "device_id": "camera-01", "occurred_at": datetime.now(timezone.utc).isoformat(),
            "state": "operational", "message": "Synthetic capture completed", **changes}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "test.db"))) as active:
        yield active


def test_health_and_openapi(client):
    assert client.get("/health/live").json() == {"status": "alive"}
    assert client.get("/health/ready").status_code == 200
    assert len(client.get("/devices").json()["items"]) == 3
    assert "/events" in client.get("/openapi.json").json()["paths"]


def test_retry_is_idempotent_and_conflicting_key_is_rejected(client):
    payload = event()
    first = client.post("/events", json=payload)
    again = client.post("/events", json=payload)
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert first.json()["event"]["id"] == again.json()["event"]["id"]
    assert client.post("/events", json={**payload, "state": "offline"}).status_code == 409
    assert client.get("/events").json()["total"] == 1


def test_concurrent_retries_create_one_row(client):
    payload = event()
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: client.post("/events", json=payload).status_code, range(8)))
    assert statuses.count(201) == 1
    assert statuses.count(200) == 7
    assert client.get("/events").json()["total"] == 1


def test_equivalent_timezone_representation_is_an_identical_retry(client):
    now = datetime.now(timezone.utc)
    payload = event(occurred_at=now.isoformat())
    assert client.post("/events", json=payload).status_code == 201
    local = now.astimezone(timezone(timedelta(hours=8))).isoformat()
    retried = client.post("/events", json={**payload, "occurred_at": local})
    assert retried.status_code == 200
    assert retried.json()["duplicate"] is True


def test_healthy_requires_all_devices_to_report_fresh_operational_events(client):
    for device in ("camera-01", "lighting-01", "edge-01"):
        assert client.post("/events", json=event(device, device_id=device)).status_code == 201
    summary = client.get("/diagnostics/summary").json()
    assert summary["overall"] == "healthy"
    assert summary["counts"]["healthy"] == 3


@pytest.mark.parametrize("changes", [
    {"state": "PASS"}, {"message": ""}, {"event_key": "bad key"},
    {"occurred_at": "2026-10-24T12:00:00"}, {"unexpected": "field"},
])
def test_validation_rejects_bad_input(client, changes):
    assert client.post("/events", json=event(**changes)).status_code == 422
    assert client.get("/events").json()["total"] == 0


def test_unknown_device_and_pagination_validation(client):
    assert client.post("/events", json=event(device_id="missing-01")).status_code == 404
    assert client.get("/events?limit=201").status_code == 422
    assert client.get("/events?offset=-1").status_code == 422


def test_filter_and_out_of_order_events(client):
    now = datetime.now(timezone.utc)
    client.post("/events", json=event("new", occurred_at=now.isoformat(), state="degraded", message="Synthetic glare"))
    client.post("/events", json=event("old", occurred_at=(now - timedelta(seconds=30)).isoformat()))
    page = client.get("/events?device_id=camera-01&state=degraded&limit=1").json()
    assert page["total"] == 1
    assert page["items"][0]["event_key"] == "new"
    camera = next(d for d in client.get("/diagnostics/summary").json()["devices"] if d["id"] == "camera-01")
    assert camera["last_event_key"] == "new"
    assert camera["diagnostic"] == "attention"


def test_diagnostics_distinguish_missing_stale_clock_skew(client):
    assert client.get("/diagnostics/summary").json()["counts"]["unknown"] == 3
    now = datetime.now(timezone.utc)
    client.post("/events", json=event("old", occurred_at=(now - timedelta(minutes=10)).isoformat()))
    client.post("/events", json=event("future", device_id="edge-01", occurred_at=(now + timedelta(minutes=10)).isoformat()))
    counts = client.get("/diagnostics/summary").json()["counts"]
    assert counts["stale"] == 1
    assert counts["clock_skew"] == 1
    assert counts["unknown"] == 1


def test_persistence_survives_app_restart(tmp_path):
    path = str(tmp_path / "persistent.db")
    with TestClient(create_app(path)) as first:
        assert first.post("/events", json=event()).status_code == 201
    with TestClient(create_app(path)) as restarted:
        assert restarted.get("/events").json()["total"] == 1


def test_storage_failure_fails_readiness_but_process_is_alive(tmp_path):
    path = str(tmp_path / "broken.db")
    with TestClient(create_app(path), raise_server_exceptions=False) as active:
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE events")
        assert active.get("/health/ready").status_code == 503
        assert active.get("/health/live").status_code == 200
        assert active.get("/events").json() == {"detail": "Storage temporarily unavailable"}
