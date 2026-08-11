"""
Local reading storage, backed by SQLite.

This replaces the previous one-CSV-file-per-cycle scheme, which produced about
8,600 files a day at the default 10 s read interval — enough to exhaust the
SD card's inodes and wear it out through constant small writes. Every cycle now
costs a single transaction against one database file.

SD-card considerations:

  * WAL journalling with ``synchronous=NORMAL``. A crash cannot corrupt the
    database; a sudden power cut may lose the last transaction or two, which
    for sensor history is a fair trade against fsync-ing on every commit.
  * Rows older than the retention window are pruned hourly, so the file
    reaches a steady size instead of growing without bound. Freed pages are
    reused by later inserts, so no VACUUM (which would rewrite the whole file)
    is needed.

Measured at ~152 bytes per row, which with 10 sensors on the default 10 s
interval works out to roughly 12 MB a day, or about 375 MB across the 30-day
retention window. Lower BOILERROOM_DATA_RETENTION_DAYS on a small card.

All SQLite calls block, so each one runs through ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from load_env import env_path
from config import GAS_SENSORS, TEMPERATURE_SENSORS
from mapping_schema import api_sensor_id

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATABASE_PATH = env_path("BOILERROOM_DATABASE", Path("data") / "readings.db")

# Readings older than this are deleted. 0 disables pruning entirely, which on
# an SD card means the file grows until something breaks.
RETENTION_DAYS = int(os.environ.get("BOILERROOM_DATA_RETENTION_DAYS", "30"))

PRUNE_INTERVAL_SECONDS = 3600

# Cap on undelivered telemetry. At roughly 1.5 KB an envelope and one a minute,
# 5,000 rows is about 7 MB and three and a half days of outage. Past that the
# oldest are dropped: an SD card that fills up takes the whole agent with it.
OUTBOX_MAX_PENDING = int(os.environ.get("BOILERROOM_OUTBOX_MAX_PENDING", "5000"))

# After this many failed deliveries a row is left behind rather than retried
# forever. Without it, one envelope the server always rejects would sit at the
# head of the queue and block everything behind it.
OUTBOX_MAX_ATTEMPTS = int(os.environ.get("BOILERROOM_OUTBOX_MAX_ATTEMPTS", "5"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY,
    captured_at   TEXT    NOT NULL,
    sensor_kind   TEXT    NOT NULL,
    sensor_index  INTEGER NOT NULL,
    sensor_id     TEXT    NOT NULL,
    role          TEXT,
    equipment_unit TEXT,
    value         REAL,
    unit          TEXT    NOT NULL,
    status        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_readings_captured_at
    ON readings (captured_at);

CREATE INDEX IF NOT EXISTS idx_readings_sensor
    ON readings (sensor_id, captured_at);

-- Telemetry that could not be delivered. The whole envelope is kept verbatim,
-- so a replay carries the original captured_at rather than pretending the
-- readings were taken when the network came back.
CREATE TABLE IF NOT EXISTS telemetry_outbox (
    id             INTEGER PRIMARY KEY,
    captured_at    TEXT    NOT NULL,
    envelope       TEXT    NOT NULL,
    sync_to_server INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    queued_at      TEXT    NOT NULL,
    synced_at      TEXT
);

-- Partial: the pending rows are the only ones ever queried in the hot path,
-- and they are a small minority once the link is healthy.
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON telemetry_outbox (id) WHERE sync_to_server = 0;
"""


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _build_rows(
    temperatures: dict[int, float | None],
    gas: dict[int, int],
    captured_at: str,
) -> list[tuple]:
    rows: list[tuple] = []

    for sensor_index, value in temperatures.items():
        cfg = TEMPERATURE_SENSORS.get(sensor_index, {})
        rows.append((
            captured_at,
            "temperature",
            sensor_index,
            api_sensor_id("temp", sensor_index, cfg),
            cfg.get("role"),
            cfg.get("unit"),
            round(value, 2) if value is not None else None,
            "C",
            "ok" if value is not None else "unavailable",
        ))

    for sensor_index, value in gas.items():
        cfg = GAS_SENSORS.get(sensor_index, {})
        rows.append((
            captured_at,
            "gas",
            sensor_index,
            api_sensor_id("gas", sensor_index, cfg),
            cfg.get("role"),
            cfg.get("unit"),
            float(value),
            "adc",
            "ok",
        ))

    return rows


class ReadingStore:
    """One SQLite connection, shared across the worker threads to_thread uses."""

    def __init__(self, path: Path | None = None):
        self.path = path or DATABASE_PATH
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._last_prune = 0.0

    # -- blocking internals, only ever called inside a worker thread ---------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(SCHEMA)
        connection.commit()
        return connection

    def _get_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = self._connect()
        return self._connection

    def _save_sync(self, rows: list[tuple]) -> None:
        with self._lock:
            connection = self._get_connection()
            with connection:  # one transaction for the whole cycle
                connection.executemany(
                    "INSERT INTO readings ("
                    "captured_at, sensor_kind, sensor_index, sensor_id, role, "
                    "equipment_unit, value, unit, status"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            self._maybe_prune_unlocked(connection)

    def _maybe_prune_unlocked(self, connection: sqlite3.Connection) -> int:
        now = time.monotonic()
        if self._last_prune and now - self._last_prune < PRUNE_INTERVAL_SECONDS:
            return 0
        self._last_prune = now

        if RETENTION_DAYS <= 0:
            return 0

        cutoff = (
            datetime.datetime.now(datetime.UTC)
            - datetime.timedelta(days=RETENTION_DAYS)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")

        with connection:
            cursor = connection.execute(
                "DELETE FROM readings WHERE captured_at < ?", (cutoff,)
            )
            # Delivered telemetry is kept only as an audit trail, and rows the
            # queue gave up on are dead weight. Both age out with the readings.
            connection.execute(
                "DELETE FROM telemetry_outbox WHERE queued_at < ? AND ("
                "sync_to_server = 1 OR attempts >= ?)",
                (cutoff, OUTBOX_MAX_ATTEMPTS),
            )
        return cursor.rowcount or 0

    def _prune_sync(self) -> int:
        with self._lock:
            self._last_prune = 0.0
            return self._maybe_prune_unlocked(self._get_connection())

    def _stats_sync(self) -> dict[str, Any]:
        with self._lock:
            connection = self._get_connection()
            row = connection.execute(
                "SELECT COUNT(*), MIN(captured_at), MAX(captured_at) FROM readings"
            ).fetchone()
            sensors = connection.execute(
                "SELECT COUNT(DISTINCT sensor_id) FROM readings"
            ).fetchone()[0]

        size = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = self.path.with_name(self.path.name + suffix)
            if candidate.exists():
                size += candidate.stat().st_size

        return {
            "path": str(self.path),
            "rows": row[0] or 0,
            "oldest": row[1],
            "newest": row[2],
            "sensors": sensors or 0,
            "size_bytes": size,
            "retention_days": RETENTION_DAYS,
        }

    # -- telemetry outbox ----------------------------------------------------

    def _enqueue_telemetry_sync(
        self, captured_at: str, envelope: str, error: str
    ) -> int:
        with self._lock:
            connection = self._get_connection()
            with connection:
                cursor = connection.execute(
                    "INSERT INTO telemetry_outbox ("
                    "captured_at, envelope, sync_to_server, attempts, "
                    "last_error, queued_at"
                    ") VALUES (?, ?, 0, 1, ?, ?)",
                    (captured_at, envelope, error[:500], _utc_now_iso()),
                )
                row_id = cursor.lastrowid

                # Drop the oldest if the queue has outgrown its cap. Newer
                # readings are the ones worth keeping: they are closer to the
                # device's present state.
                overflow = connection.execute(
                    "SELECT COUNT(*) FROM telemetry_outbox WHERE sync_to_server = 0"
                ).fetchone()[0] - OUTBOX_MAX_PENDING
                if overflow > 0:
                    connection.execute(
                        "DELETE FROM telemetry_outbox WHERE id IN ("
                        "SELECT id FROM telemetry_outbox WHERE sync_to_server = 0 "
                        "ORDER BY id LIMIT ?)",
                        (overflow,),
                    )
            return row_id

    def _pending_telemetry_sync(self, limit: int) -> list[tuple[int, str, str]]:
        with self._lock:
            connection = self._get_connection()
            return [
                (row[0], row[1], row[2])
                for row in connection.execute(
                    "SELECT id, captured_at, envelope FROM telemetry_outbox "
                    "WHERE sync_to_server = 0 AND attempts < ? "
                    "ORDER BY id LIMIT ?",
                    (OUTBOX_MAX_ATTEMPTS, limit),
                ).fetchall()
            ]

    def _mark_synced_sync(self, row_id: int) -> None:
        with self._lock:
            connection = self._get_connection()
            with connection:
                connection.execute(
                    "UPDATE telemetry_outbox "
                    "SET sync_to_server = 1, synced_at = ?, last_error = NULL "
                    "WHERE id = ?",
                    (_utc_now_iso(), row_id),
                )

    def _record_attempt_sync(self, row_id: int, error: str) -> int:
        with self._lock:
            connection = self._get_connection()
            with connection:
                connection.execute(
                    "UPDATE telemetry_outbox "
                    "SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                    (error[:500], row_id),
                )
            return connection.execute(
                "SELECT attempts FROM telemetry_outbox WHERE id = ?", (row_id,)
            ).fetchone()[0]

    def _outbox_stats_sync(self) -> dict[str, Any]:
        with self._lock:
            connection = self._get_connection()
            pending, oldest = connection.execute(
                "SELECT COUNT(*), MIN(captured_at) FROM telemetry_outbox "
                "WHERE sync_to_server = 0 AND attempts < ?",
                (OUTBOX_MAX_ATTEMPTS,),
            ).fetchone()
            stuck = connection.execute(
                "SELECT COUNT(*) FROM telemetry_outbox "
                "WHERE sync_to_server = 0 AND attempts >= ?",
                (OUTBOX_MAX_ATTEMPTS,),
            ).fetchone()[0]
            synced = connection.execute(
                "SELECT COUNT(*) FROM telemetry_outbox WHERE sync_to_server = 1"
            ).fetchone()[0]

        return {
            "pending": pending or 0,
            "oldest_pending": oldest,
            "stuck": stuck or 0,
            "synced": synced or 0,
            "max_pending": OUTBOX_MAX_PENDING,
            "max_attempts": OUTBOX_MAX_ATTEMPTS,
        }

    def _recent_sync(self, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            connection = self._get_connection()
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT captured_at, sensor_id, value, unit, status "
                "FROM readings ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def _close_sync(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    # -- async surface -------------------------------------------------------

    async def save(
        self,
        temperatures: dict[int, float | None],
        gas: dict[int, int],
    ) -> None:
        rows = _build_rows(temperatures, gas, _utc_now_iso())
        if rows:
            await asyncio.to_thread(self._save_sync, rows)

    async def prune(self) -> int:
        return await asyncio.to_thread(self._prune_sync)

    async def stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._stats_sync)

    async def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._recent_sync, limit)

    async def enqueue_telemetry(
        self, captured_at: str, envelope: str, error: str
    ) -> int:
        return await asyncio.to_thread(
            self._enqueue_telemetry_sync, captured_at, envelope, error
        )

    async def pending_telemetry(self, limit: int) -> list[tuple[int, str, str]]:
        return await asyncio.to_thread(self._pending_telemetry_sync, limit)

    async def mark_telemetry_synced(self, row_id: int) -> None:
        await asyncio.to_thread(self._mark_synced_sync, row_id)

    async def record_telemetry_attempt(self, row_id: int, error: str) -> int:
        return await asyncio.to_thread(self._record_attempt_sync, row_id, error)

    async def outbox_stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._outbox_stats_sync)

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)


reading_store = ReadingStore()


async def save_readings(
    temperatures: dict[int, float | None],
    gas: dict[int, int],
) -> None:
    """Persist one cycle of readings without blocking the event loop."""
    await reading_store.save(temperatures, gas)
