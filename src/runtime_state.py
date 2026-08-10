"""Shared runtime state for sensor loop and control menu."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

from logging_setup import get_logger, split_tag

# Telemetry cadence used until the server pushes a config. Posting every sensor
# cycle would be one upload every 10 s — far more than the platform asks for,
# and needless radio time on a Pi Zero W.
DEFAULT_TELEMETRY_INTERVAL_SECONDS = 60.0


class RuntimeState:
    def __init__(self, read_interval: float = 10.0):
        self.shutdown = asyncio.Event()
        # Set once a device session exists. Until then the agent runs offline
        # from its cached schedule; telemetry and the WebSocket wait for it.
        self.authenticated = asyncio.Event()
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
        self.last_processed_command_id: str | None = None

        # Set by device.restart_service so the session drops and reconnects.
        self.ws_reconnect_requested = asyncio.Event()

        self._control_lock = asyncio.Lock()
        self._modes: dict[Any, str] = {}
        self._limit_blocks: dict[Any, str] = {}
        self.device_config = None

    async def wait_authenticated(self) -> bool:
        """Block until a session exists. Returns False if shutdown came first."""
        if self.authenticated.is_set():
            return True

        waiters = [
            asyncio.create_task(self.authenticated.wait()),
            asyncio.create_task(self.shutdown.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()
        return self.authenticated.is_set()

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

    async def set_active_schedule_version(self, version: int) -> None:
        async with self._ws_lock:
            self.active_schedule_version = version

    async def set_active_config_version(self, version: int) -> None:
        async with self._ws_lock:
            self.active_config_version = version

    async def get_heartbeat_interval(self) -> float:
        async with self._ws_lock:
            return float(self.heartbeat_interval_seconds or 30)

    async def set_last_processed_command_id(self, command_id: str) -> None:
        async with self._ws_lock:
            self.last_processed_command_id = command_id

    async def get_last_processed_command_id(self) -> str | None:
        async with self._ws_lock:
            return self.last_processed_command_id

    async def request_ws_reconnect(self) -> None:
        self.ws_reconnect_requested.set()

    # -- control mode (automatic = schedule driven, manual = command driven) --

    async def set_mode(self, target: Any, mode: str) -> None:
        async with self._control_lock:
            self._modes[target] = mode

    async def get_mode(self, target: Any) -> str:
        async with self._control_lock:
            return self._modes.get(target, "automatic")

    async def get_modes(self) -> dict[Any, str]:
        async with self._control_lock:
            return dict(self._modes)

    # -- safety cut-outs (set by the limit guard, respected by everything) ----

    async def set_limit_block(self, target: Any, reason: str) -> None:
        async with self._control_lock:
            self._limit_blocks[target] = reason

    async def clear_limit_block(self, target: Any) -> None:
        async with self._control_lock:
            self._limit_blocks.pop(target, None)

    async def is_limit_blocked(self, target: Any) -> bool:
        async with self._control_lock:
            return target in self._limit_blocks

    async def get_limit_blocks(self) -> dict[Any, str]:
        async with self._control_lock:
            return dict(self._limit_blocks)

    # -- pushed configuration ------------------------------------------------

    async def set_device_config(self, config: Any) -> None:
        async with self._control_lock:
            self.device_config = config

    async def get_device_config(self) -> Any:
        async with self._control_lock:
            return self.device_config

    async def get_telemetry_interval(self) -> float:
        """
        Telemetry cadence in seconds.

        The server's ``telemetry_interval_seconds`` wins when a config has been
        pushed; otherwise the one-minute default applies. Never None — the
        sensor loop always paces its uploads.
        """
        async with self._control_lock:
            config = self.device_config
        interval = getattr(config, "telemetry_interval_seconds", None)
        if not interval:
            return DEFAULT_TELEMETRY_INTERVAL_SECONDS
        return float(interval)

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

    async def log(self, *args, level: int = logging.INFO, **_kwargs) -> None:
        """
        Emit a log record.

        Messages carry a ``[tag]`` prefix by convention; the tag selects the
        logger (``[ws]`` -> ``edge.ws``) so verbosity is tunable per subsystem.
        Nothing here blocks: the record goes onto a queue that a listener
        thread drains.
        """
        message = " ".join(str(arg) for arg in args)
        tag, text = split_tag(message)
        get_logger(tag).log(level, text)

    async def echo(self, text: str = "") -> None:
        """
        Write straight to the terminal, creating no log record.

        Used for output that must not be logged — showing the log file through
        state.log() would append what you are reading back into the file.
        """
        async with self.print_lock:
            await asyncio.to_thread(print, text)

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
