from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("notifier.main")

DB_PATH = os.getenv("NOTIFIER_DB", "/data/notifier.db")

# fault modes
#   normal            -> accept and return receipt
#   drop_receipt      -> persist + count as accepted, but respond 504 (receipt lost)
#   reject            -> do not persist, respond 503 (not accepted)
VALID_MODES = {"normal", "drop_receipt", "reject"}

# Per-notification lifecycle:
#   accepted  -> the notification number has been accepted at least once
#   cancelled -> tombstone: the alarm side closed the round as cancelled; any
#                late /notify for this number must be refused (HTTP 410)
STATE_ACCEPTED = "accepted"
STATE_CANCELLED = "cancelled"


class NotifyIn(BaseModel):
    notification_no: str
    device: str | None = None
    round_id: int | None = None
    triggered_at: float | None = None
    recovered_at: float | None = None
    maintenance_id: int | None = None
    message: str | None = None


class FaultIn(BaseModel):
    mode: str


class NotifierStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._mode = "normal"
        with self._lock, self._db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    notification_no TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'accepted',
                    accepted_at REAL,
                    cancelled_at REAL,
                    device TEXT,
                    round_id INTEGER,
                    triggered_at REAL,
                    recovered_at REAL,
                    maintenance_id INTEGER,
                    message TEXT,
                    deliveries INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            # Migration for databases created before cancellation tombstones existed.
            existing_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(notifications)")
            }
            if "state" not in existing_cols:
                conn.execute(
                    "ALTER TABLE notifications ADD COLUMN state TEXT NOT NULL DEFAULT 'accepted'"
                )
            if "cancelled_at" not in existing_cols:
                conn.execute("ALTER TABLE notifications ADD COLUMN cancelled_at REAL")
            if "accepted_at" not in existing_cols:
                conn.execute("ALTER TABLE notifications ADD COLUMN accepted_at REAL")
            # Old schemas declared accepted_at NOT NULL, which forbids cancellation
            # tombstones (they have no acceptance time). Rebuild the table to relax it.
            col_info = conn.execute("PRAGMA table_info(notifications)").fetchall()
            accepted_notnull = any(
                row["name"] == "accepted_at" and row["notnull"] == 1 for row in col_info
            )
            if accepted_notnull:
                conn.execute("ALTER TABLE notifications RENAME TO notifications_legacy")
                conn.execute(
                    """
                    CREATE TABLE notifications (
                        notification_no TEXT PRIMARY KEY,
                        state TEXT NOT NULL DEFAULT 'accepted',
                        accepted_at REAL,
                        cancelled_at REAL,
                        device TEXT,
                        round_id INTEGER,
                        triggered_at REAL,
                        recovered_at REAL,
                        maintenance_id INTEGER,
                        message TEXT,
                        deliveries INTEGER NOT NULL DEFAULT 1
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO notifications
                        (notification_no, state, accepted_at, cancelled_at, device, round_id,
                         triggered_at, recovered_at, maintenance_id, message, deliveries)
                    SELECT notification_no,
                           COALESCE(state, 'accepted'),
                           accepted_at,
                           cancelled_at,
                           device, round_id, triggered_at, recovered_at, maintenance_id,
                           message, deliveries
                      FROM notifications_legacy
                    """
                )
                conn.execute("DROP TABLE notifications_legacy")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'mode'"
            ).fetchone()
            if row is not None and row["value"] in VALID_MODES:
                self._mode = str(row["value"])

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> str:
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('mode', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (mode,),
            )
            self._mode = mode
        return mode

    def accept(self, item: NotifyIn) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._db() as conn:
            existing = conn.execute(
                "SELECT * FROM notifications WHERE notification_no = ?",
                (item.notification_no,),
            ).fetchone()
            if existing is not None and existing["state"] == STATE_CANCELLED:
                # A late delivery for an already-cancelled round must never be accepted.
                return {"accepted": False, "cancelled": True}
            if existing is not None:
                conn.execute(
                    "UPDATE notifications SET deliveries = deliveries + 1 WHERE notification_no = ?",
                    (item.notification_no,),
                )
                return {
                    "accepted": True,
                    "accepted_at": existing["accepted_at"],
                    "duplicate": True,
                }
            conn.execute(
                """
                INSERT INTO notifications
                    (notification_no, state, accepted_at, device, round_id, triggered_at,
                     recovered_at, maintenance_id, message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.notification_no,
                    STATE_ACCEPTED,
                    now,
                    item.device,
                    item.round_id,
                    item.triggered_at,
                    item.recovered_at,
                    item.maintenance_id,
                    item.message,
                ),
            )
            return {"accepted": True, "accepted_at": now, "duplicate": False}

    def cancel(self, notification_no: str) -> dict[str, Any]:
        """Register a cancellation tombstone.

        Idempotent and safe against an in-flight acceptance: if the number was
        already accepted it keeps its accepted record (the caller reconciles
        via the returned state); otherwise a tombstone is written so any late
        notify is refused.
        """
        now = time.time()
        with self._lock, self._db() as conn:
            existing = conn.execute(
                "SELECT * FROM notifications WHERE notification_no = ?",
                (notification_no,),
            ).fetchone()
            if existing is not None:
                return {
                    "state": existing["state"],
                    "accepted_at": existing["accepted_at"],
                    "cancelled_at": existing["cancelled_at"],
                }
            conn.execute(
                """
                INSERT INTO notifications
                    (notification_no, state, accepted_at, cancelled_at, deliveries)
                VALUES (?, ?, NULL, ?, 0)
                """,
                (notification_no, STATE_CANCELLED, now),
            )
            return {"state": STATE_CANCELLED, "accepted_at": None, "cancelled_at": now}

    def lookup(self, notification_no: str) -> dict[str, Any] | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM notifications WHERE notification_no = ?",
                (notification_no,),
            ).fetchone()
            return dict(row) if row else None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM notifications ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]


def create_app(store: NotifierStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.store is None:
            app.state.store = NotifierStore(DB_PATH)
            initial_mode = os.getenv("NOTIFIER_MODE", "normal")
            if initial_mode in VALID_MODES:
                app.state.store.set_mode(initial_mode)
        yield

    app = FastAPI(title="本地通知端（故障注入）", lifespan=lifespan)
    app.state.store = store

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "mode": app.state.store.mode}

    @app.post("/notify")
    def notify(item: NotifyIn, request: Request) -> JSONResponse:
        store: NotifierStore = request.app.state.store
        mode = store.mode
        if mode == "reject":
            logger.info("reject %s (fault mode)", item.notification_no)
            return JSONResponse(
                status_code=503,
                content={"accepted": False, "detail": "模拟通知端故障，未受理"},
            )
        if not item.notification_no.strip():
            return JSONResponse(status_code=422, content={"accepted": False})
        result = store.accept(item)
        if result.get("cancelled"):
            # Cancellation tombstone wins regardless of the current fault mode:
            # a late delivery of a closed round can never be accepted.
            logger.info("refuse late notify %s (cancelled tombstone)", item.notification_no)
            return JSONResponse(
                status_code=410,
                content={
                    "accepted": False,
                    "cancelled": True,
                    "detail": "该通知号对应轮次已取消，迟到的通知不再受理",
                },
            )
        if mode == "drop_receipt":
            logger.info("accepted %s then dropping receipt (fault mode)", item.notification_no)
            return JSONResponse(
                status_code=504,
                content={"accepted": True, "detail": "模拟已受理但回执丢失"},
            )
        logger.info("accepted %s", item.notification_no)
        return JSONResponse(status_code=200, content=result)

    @app.post("/notifications/{notification_no}/cancel")
    def cancel_notification(notification_no: str, request: Request) -> JSONResponse:
        store: NotifierStore = request.app.state.store
        result = store.cancel(notification_no)
        if result["state"] == STATE_ACCEPTED:
            # An in-flight acceptance landed first: the round must be reconciled
            # as sent instead of cancelled. The accepted record is untouched.
            return JSONResponse(status_code=409, content=result)
        return JSONResponse(status_code=200, content=result)

    @app.get("/notifications/{notification_no}")
    def lookup(notification_no: str, request: Request) -> JSONResponse:
        row = request.app.state.store.lookup(notification_no)
        if row is None:
            return JSONResponse(
                status_code=404,
                content={"accepted": False, "detail": "通知端没有该通知号"},
            )
        if row["state"] == STATE_CANCELLED:
            return JSONResponse(
                status_code=410,
                content={
                    "accepted": False,
                    "cancelled": True,
                    "notification_no": row["notification_no"],
                    "cancelled_at": row["cancelled_at"],
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "accepted": True,
                "accepted_at": row["accepted_at"],
                "notification_no": row["notification_no"],
                "deliveries": row["deliveries"],
            },
        )

    @app.get("/api/notifications")
    def list_notifications(request: Request) -> list[dict[str, Any]]:
        return request.app.state.store.recent()

    @app.post("/api/fault")
    def set_fault(body: FaultIn, request: Request) -> dict[str, Any]:
        try:
            mode = request.app.state.store.set_mode(body.mode)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        return {"ok": True, "mode": mode}

    return app


app = create_app()
