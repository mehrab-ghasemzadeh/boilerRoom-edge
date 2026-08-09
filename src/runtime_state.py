"""Shared runtime state for sensor loop and control menu."""

from __future__ import annotations

import asyncio
import datetime
from typing import Any


class RuntimeState:
    def __init__(self, read_interval: float = 10.0):
        self.shutdown = asyncio.Event()
        self.print_lock = asyncio.Lock()
        self.read_interval = read_interval

        self._data_lock = asyncio.Lock()
        self._last_temperatures: dict[int, float | None] = {}
        self._last_gas: dict[int, int] = {}
        self._last_read_at: datetime.datetime | None = None
        self._cycle_count = 0

        self.relay_controller = None
        self.gas_reader = None
        self.temperature_reader = None

        self._ws_lock = asyncio.Lock()
        self.ws_connected = False
        self.hello_ack: dict[str, Any] | None = None
        self.active_config_version = 0
        self.active_schedule_version = 0
        self.heartbeat_interval_seconds = 30

    async def set_ws_connected(self, connected: bool) -> None:
        async with self._ws_lock:
            self.ws_connected = connected

    async def set_hello_ack(self, message: dict[str, Any]) -> None:
        async with self._ws_lock:
            self.hello_ack = message
            payload = message.get("payload", {})
            self.heartbeat_interval_seconds = payload.get("heartbeat_interval_seconds", 30)

    async def get_active_versions(self) -> tuple[int, int]:
        async with self._ws_lock:
            return self.active_config_version, self.active_schedule_version

    async def get_ws_status(self) -> dict[str, Any]:
        async with self._ws_lock:
            ack_payload = (self.hello_ack or {}).get("payload", {})
            return {
                "connected": self.ws_connected,
                "hello_ack": self.hello_ack is not None,
                "server_time": ack_payload.get("server_time"),
                "desired_config_version": ack_payload.get("desired_config_version"),
                "desired_schedule_version": ack_payload.get("desired_schedule_version"),
                "heartbeat_interval_seconds": ack_payload.get("heartbeat_interval_seconds"),
            }

    async def log(self, *args, **kwargs) -> None:
        async with self.print_lock:
            print(*args, **kwargs)

    async def update_readings(
        self,
        temperatures: dict[int, float | None],
        gas: dict[int, int],
    ) -> None:
        async with self._data_lock:
            self._last_temperatures = dict(temperatures)
            self._last_gas = dict(gas)
            self._last_read_at = datetime.datetime.now(datetime.UTC)
            self._cycle_count += 1

    async def get_snapshot(self) -> dict[str, Any]:
        async with self._data_lock:
            return {
                "temperatures": dict(self._last_temperatures),
                "gas": dict(self._last_gas),
                "read_at": self._last_read_at,
                "cycle_count": self._cycle_count,
            }

    async def get_read_interval(self) -> float:
        return self.read_interval

    async def set_read_interval(self, seconds: float) -> None:
        self.read_interval = max(1.0, seconds)
