"""Exercise a running local API and save real response evidence (stdlib only)."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
output = Path(__file__).resolve().parents[1] / "evidence" / "walkthrough.json"
records = []


def request(label, path, expected, payload=None):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload else None,
                                 headers={"Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    body = json.loads(response.read())
    records.append({"scenario": label, "method": "POST" if payload else "GET", "path": path,
                    "expected_status": expected, "actual_status": response.status, "response": body})
    assert response.status == expected, (label, response.status, body)


now = datetime.now(timezone.utc)
run_id = uuid.uuid4().hex[:10]
payload = {"event_key": f"demo-{run_id}", "device_id": "camera-01", "occurred_at": now.isoformat(),
           "state": "degraded", "message": "Synthetic glare detected; operator review recommended"}
request("Liveness", "/health/live", 200)
request("Readiness", "/health/ready", 200)
request("New event", "/events", 201, payload)
request("Safe identical retry", "/events", 200, payload)
request("Conflicting retry", "/events", 409, {**payload, "state": "offline"})
request("Invalid state", "/events", 422, {**payload, "event_key": f"invalid-{run_id}", "state": "PASS"})
request("Unknown device", "/events", 404, {**payload, "event_key": f"unknown-{run_id}", "device_id": "missing-01"})
request("Stale event", "/events", 201, {**payload, "event_key": f"stale-{run_id}", "device_id": "lighting-01",
        "occurred_at": (now - timedelta(minutes=10)).isoformat(), "state": "operational", "message": "Synthetic historical heartbeat"})
request("Fresh event", "/events", 201, {**payload, "event_key": f"fresh-{run_id}", "device_id": "edge-01",
        "state": "operational", "message": "Synthetic edge heartbeat"})
request("Filtered history", "/events?device_id=camera-01&state=degraded&limit=5", 200)
request("Diagnostic summary", "/diagnostics/summary", 200)
output.parent.mkdir(exist_ok=True)
output.write_text(json.dumps({"recorded_at": now.isoformat(), "base_url": base, "synthetic": True,
                             "passed": len(records), "scenarios": records}, indent=2) + "\n")
print(f"PASS: {len(records)} live HTTP scenarios; evidence: {output}")
