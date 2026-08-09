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

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)


reading_store = ReadingStore()


async def save_readings(
    temperatures: dict[int, float | None],
    gas: dict[int, int],
) -> None:
    """Persist one cycle of readings without blocking the event loop."""
    await reading_store.save(temperatures, gas)
