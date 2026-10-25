# Operations Health API

![API overview](docs/operations-health-api.png)

[Case study PDF](docs/case-study.pdf) · [API documentation screenshot](docs/operations-health-api-docs.jpg)

An independent, **AI-assisted portfolio demonstration created in October 2026**, using synthetic camera, lighting and edge-controller events. It was prepared with Codex assistance during portfolio development. It is not a previous employer/client deliverable, not a production deployment, and not evidence of unaided personal authorship. The owner should be able to explain, run and adapt it before presenting it as a skills sample.

The small project demonstrates API integration decisions that are easy to inspect: explicit input contracts, durable event storage, retry behavior, out-of-order history, health checks and actionable diagnostics. It does not connect to real inspection equipment or call a paid AI service.

## Run locally

Python 3.12 is recommended. From this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open [interactive API documentation](http://127.0.0.1:8765/docs) or [diagnostic JSON](http://127.0.0.1:8765/diagnostics/summary). SQLite initializes on startup at `data/operations.db`. Set `OPERATIONS_DB_PATH` to choose a different persistent file and `STALE_AFTER_SECONDS` to set freshness (default 300 seconds). `.env.example` is a reference: local Uvicorn does not automatically load it.

## Guided demo

With the API running, run in another terminal:

```sh
.venv/bin/python scripts/walkthrough.py http://127.0.0.1:8765
```

It sends synthetic events and records real HTTP status codes/responses in `evidence/walkthrough.json`: new event **201**, identical retry **200**, conflicting retry **409**, invalid input **422**, unknown device **404**, then filtered history and diagnostics. Three device examples show a fresh edge heartbeat, camera degradation and a stale lighting heartbeat. The script adds a unique key per run; it never clears stored history. Generated timestamps naturally become stale after five minutes.

```sh
curl http://127.0.0.1:8765/health/ready
curl 'http://127.0.0.1:8765/events?device_id=camera-01&limit=5'
curl http://127.0.0.1:8765/diagnostics/summary
```

## API contract

| Endpoint | Purpose |
|---|---|
| `GET /health/live` | Confirms that the API process responds; independent of storage |
| `GET /health/ready` | Reads required database tables; storage failure returns 503 |
| `GET /devices` | Lists three seeded synthetic devices |
| `POST /events` | Validates and persists an event, with globally unique `event_key` |
| `GET /events` | Filters by device/state; bounded limit/offset pagination |
| `GET /diagnostics/summary` | Derives latest device state, freshness and aggregate counts |
| `GET /docs`, `GET /openapi.json` | Interactive contract and machine-readable API schema |

Required event fields: `event_key`, `device_id`, timezone-aware `occurred_at`, `state` (`operational`, `degraded`, `offline`), and bounded `message`. Unknown fields are rejected. The client must reuse the exact semantic payload and event key when retrying. Timestamps normalize to UTC; equivalent timezone representations are identical. A changed payload with the same key returns 409 rather than silently overwriting.

The database writer transaction serializes key-check/insertion, preventing duplicate rows during concurrent retries. Every request opens a short-lived SQLite connection. WAL mode permits readers while a writer commits; a five-second lock timeout bounds contention. All query values use bound parameters.

Diagnostics select by **event occurrence time**, then ingestion ID to break equal-time ties. Late-arriving historical events are retained without replacing a newer status. A future timestamp more than 60 seconds ahead produces `clock_skew`; a report older than the threshold is `stale`; non-operational fresh reports need `attention`; missing reports are `unknown`. `healthy` requires all devices to be fresh and operational. These are synthetic operational indicators, not assembly PASS/FAIL or a machine safety interlock.

## Tests

```sh
.venv/bin/python -m pytest -q
```

Meaningful integration cases cover persistent data across app restart, eight concurrent identical retries, key conflicts, malformed input, unknown devices, pagination bounds, filters, late events, stale/missing/future timestamps and storage failure that makes readiness fail while liveness still succeeds. Tests create temporary databases and never modify the demo database.

Verified locally on Python **3.12.14**: **15 tests passed**, and the running Uvicorn API passed **11 live HTTP scenarios**. FastAPI 0.162.0, Uvicorn 0.54.0, Pydantic 2.14.1, Starlette 1.9.0, HTTPX 0.28.1 and pytest 9.1.1 were used. Starlette emitted one deprecation warning about future TestClient support for HTTPX; the tests still passed. The initial local run did not execute Docker because its daemon was unavailable. The original published source subsequently passed [GitHub CI](https://github.com/smes112108/engineering-portfolio/actions/runs/40085770363), including a Docker build, readiness check and 11 HTTP scenarios. This standalone repository has its own CI workflow; check its current result in the Actions tab.

`requirements.txt` pins the tested runtime dependency closure; `requirements-dev.txt` adds the test tooling. To recreate the **complete 22-package development environment** exactly at the recorded package versions, use:

```sh
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pytest -q
```

The lock records installed versions, not artifact hashes or a frozen operating system. Full observed statuses and timestamps are in `evidence/verification.json` and `evidence/walkthrough.json`.

## Container packaging

```sh
docker compose up --build -d
docker compose ps
python3 scripts/walkthrough.py http://127.0.0.1:8000
docker compose down
```

Compose binds only `127.0.0.1:8000`, stores SQLite in a named volume and runs the image as UID 10001 with a read-only root filesystem, dropped capabilities and no-new-privileges. `down` preserves the named data volume. The image has an HTTP readiness health check. Review `evidence/verification.json` for what was actually executed: having a Dockerfile does not by itself establish a successful container build or runtime verification.

## Scope and limitations

This is a single-node local demo, with a fixed device registry. There is no authentication, authorization, TLS termination, rate limiting, data retention, schema migration framework, exporter, external message broker or cloud deployment. Keep it on loopback. A real deployment would need those controls plus backup/restore verification and workload-specific load testing. SQLite and serialized writes are intentionally bounded; no distributed or high-throughput claim is made. The Python base image tag is floating, so rebuilds are not byte-for-byte reproducible. Application dependencies are pinned; see the verification evidence for the exact versions used.

Useful discussion topics: why idempotency is an ingestion concern, why readiness differs from liveness, what happens to out-of-order events, why occurrence time can be untrustworthy, and how this would evolve toward authenticated devices, PostgreSQL, a broker and monitored workers.
