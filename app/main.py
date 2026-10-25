"""Operations Health API: a small, durable and inspectable integration demo."""

from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

DEVICES = (
    ("camera-01", "Inspection camera", "camera"),
    ("lighting-01", "Inspection lighting", "lighting"),
    ("edge-01", "Edge controller", "edge"),
)
State = Literal["operational", "degraded", "offline"]


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    device_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")
    occurred_at: AwareDatetime
    state: State
    message: str = Field(min_length=1, max_length=1000)


class EventOut(BaseModel):
    id: int
    event_key: str
    device_id: str
    occurred_at: str
    received_at: str
    state: State
    message: str


class IngestResult(BaseModel):
    duplicate: bool
    event: EventOut


class EventPage(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def connect(path: str):
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def create_app(database_path: str | None = None, stale_seconds: int | None = None) -> FastAPI:
    db_path = database_path or os.getenv("OPERATIONS_DB_PATH", "data/operations.db")
    stale_after = stale_seconds if stale_seconds is not None else int(os.getenv("STALE_AFTER_SECONDS", "300"))
    if stale_after < 1:
        raise ValueError("STALE_AFTER_SECONDS must be positive")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with connect(db_path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    device_id TEXT NOT NULL REFERENCES devices(id),
                    occurred_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('operational','degraded','offline')),
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_device_time ON events(device_id, occurred_at DESC, id DESC);
            """)
            db.executemany("INSERT OR IGNORE INTO devices(id,name,kind) VALUES (?,?,?)", DEVICES)
        yield

    application = FastAPI(
        title="Operations Health API",
        version="1.0.0",
        description=("Independent AI-assisted portfolio demo using synthetic device events. "
                     "Inspect validation, durable ingestion, idempotent retries and freshness diagnostics. "
                     "No real equipment or external AI service is connected."),
        lifespan=lifespan,
    )

    @application.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        logging.getLogger("operations").error("Database operation failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Storage temporarily unavailable"})

    @application.get("/", tags=["About"])
    def index():
        return {"service": "Operations Health API", "version": "1.0.0", "data": "synthetic", "docs": "/docs"}

    @application.get("/health/live", tags=["Health"])
    def live():
        return {"status": "alive"}

    @application.get("/health/ready", tags=["Health"])
    def ready():
        with connect(db_path) as db:
            db.execute("SELECT id FROM devices LIMIT 1").fetchone()
            db.execute("SELECT id FROM events LIMIT 1").fetchone()
        return {"status": "ready", "storage": "sqlite"}

    @application.get("/devices", tags=["Devices"])
    def devices():
        with connect(db_path) as db:
            return {"items": [dict(row) for row in db.execute("SELECT * FROM devices ORDER BY id")]}

    @application.post("/events", response_model=IngestResult, status_code=201, tags=["Events"],
                      responses={200: {"description": "Identical event already received"},
                                 404: {"description": "Unknown device"},
                                 409: {"description": "Event key reused for a different payload"}})
    def ingest(event: EventIn, response: Response):
        normalized = event.model_dump(mode="json")
        normalized["occurred_at"] = event.occurred_at.astimezone(timezone.utc).isoformat(timespec="microseconds")
        payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        with connect(db_path) as db:
            # A single writer transaction makes check+insert safe for concurrent retries.
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM events WHERE event_key=?", (event.event_key,)).fetchone()
            if previous:
                if previous["payload"] != payload:
                    raise HTTPException(409, "event_key already belongs to a different payload")
                response.status_code = 200
                return {"duplicate": True, "event": dict(previous)}
            if not db.execute("SELECT 1 FROM devices WHERE id=?", (event.device_id,)).fetchone():
                raise HTTPException(404, "Unknown device_id; inspect GET /devices")
            cursor = db.execute(
                "INSERT INTO events(event_key,device_id,occurred_at,received_at,state,message,payload) VALUES (?,?,?,?,?,?,?)",
                (event.event_key, event.device_id, normalized["occurred_at"], utc_now().isoformat(), event.state, event.message, payload),
            )
            created = db.execute("SELECT * FROM events WHERE id=?", (cursor.lastrowid,)).fetchone()
            return {"duplicate": False, "event": dict(created)}

    @application.get("/events", response_model=EventPage, tags=["Events"])
    def events(device_id: str | None = None, state: State | None = None,
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        terms, parameters = [], []
        if device_id is not None:
            terms.append("device_id=?")
            parameters.append(device_id)
        if state is not None:
            terms.append("state=?")
            parameters.append(state)
        where = " WHERE " + " AND ".join(terms) if terms else ""
        with connect(db_path) as db:
            total = db.execute("SELECT COUNT(*) FROM events" + where, parameters).fetchone()[0]
            rows = db.execute("SELECT * FROM events" + where + " ORDER BY occurred_at DESC,id DESC LIMIT ? OFFSET ?",
                              [*parameters, limit, offset]).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    @application.get("/diagnostics/summary", tags=["Diagnostics"])
    def summary():
        now = utc_now()
        results = []
        with connect(db_path) as db:
            for device in db.execute("SELECT * FROM devices ORDER BY id").fetchall():
                latest = db.execute("SELECT * FROM events WHERE device_id=? ORDER BY occurred_at DESC,id DESC LIMIT 1",
                                    (device["id"],)).fetchone()
                if latest is None:
                    results.append({**dict(device), "reported_state": None, "diagnostic": "unknown", "last_event_key": None,
                                    "age_seconds": None, "reason": "No events received"})
                    continue
                age = (now - datetime.fromisoformat(latest["occurred_at"])).total_seconds()
                if age < -60:
                    diagnostic, reason = "clock_skew", "Latest event timestamp is more than 60 seconds in the future"
                elif age > stale_after:
                    diagnostic, reason = "stale", "Latest event exceeds the freshness threshold"
                elif latest["state"] != "operational":
                    diagnostic, reason = "attention", latest["message"]
                else:
                    diagnostic, reason = "healthy", latest["message"]
                results.append({**dict(device), "reported_state": latest["state"], "diagnostic": diagnostic,
                                "last_event_key": latest["event_key"], "age_seconds": round(age, 3), "reason": reason})
        counts = {key: sum(item["diagnostic"] == key for item in results)
                  for key in ("healthy", "attention", "stale", "unknown", "clock_skew")}
        return {"generated_at": now.isoformat(), "stale_after_seconds": stale_after,
                "overall": "healthy" if counts["healthy"] == len(results) else "attention_required",
                "counts": counts, "devices": results}

    return application


app = create_app()
