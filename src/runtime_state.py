"""Shared runtime state for sensor loop and control menu."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any

from logging_setup import get_logger, split_tag

# Telemetry cadence used until the server pushes a config. Posting every sensor
# cycle would be one upload every 10 s — far more than the platform asks for,
# and needless radio time on a Pi Zero W.
DEFAULT_TELEMETRY_INTERVAL_SECONDS = 60.0

# How early a read cycle may post and still count as on time. See telemetry_due.
TELEMETRY_DUE_SLACK_SECONDS = 5.0


class RuntimeState:
    def __init__(self, read_interval: float = 10.0, write_interval: float = 60.0):
        self.shutdown = asyncio.Event()
        self.authenticated = asyncio.Event()
        self.credentials_needed = asyncio.Event()
        self.credentials_ready = asyncio.Event()
        self.mapping_ready = asyncio.Event()
        self.print_lock = asyncio.Lock()
        self.read_interval = read_interval
        self.write_interval = write_interval

        self._data_lock = asyncio.Lock()
        self._last_temperatures: dict[int, float | None] = {}
        self._last_gas: dict[int, int] = {}
        self._last_read_at: datetime.datetime | None = None
        self._cycle_count = 0

        self.relay_controller = None
        self.gas_reader = None
        self.temperature_reader = None
        # What the control menu is answered on: the GPIO matrix keypad on the
        # installed device, the keyboard on the bench. Set by main from the
        # same USE_MOCK_HARDWARE switch as the readers above.
        self.keypad = None
        # Where it is shown: the ST7920 panel on the installed device, nothing
        # at all on the bench unless asked for. Set by main from the same
        # switch.
        self.display = None

        # When set, echo() collects lines here instead of printing them. The
        # menu turns this on so the output written for a terminal can be shown
        # on the panel a screen at a time — six rows of it does not scroll past
        # and cannot be paged back to.
        self._echo_sink: list[str] | None = None

        self._ws_lock = asyncio.Lock()
        self.ws_connected = False
        self.hello_ack: dict[str, Any] | None = None
        self.active_config_version = 0
        self.active_schedule_version = 0
        self.heartbeat_interval_seconds = 30
        self.last_processed_command_id: str | None = None

        # Set by device.restart_service so the session drops and reconnects.
        self.ws_reconnect_requested = asyncio.Event()

        # Asks the device-record loop to poll now instead of at its next tick.
        # Used after reporting modes upstream: the record is what reconciles
        # them, and waiting out the 15-minute interval to find out leaves the
        # device on the wrong modes for the whole of it.
        self.record_refresh_requested = asyncio.Event()

        # Held while a record is being adopted. The record loop and the
        # post-hello reconciler both fetch and apply one, and adoption is not
        # atomic — it sets modes, setpoints, calibration and the mapping in
        # turn — so without this a reconnect landing on a poll could apply
        # halves of two different records.
        self.record_lock = asyncio.Lock()

        self._control_lock = asyncio.Lock()
        self._modes: dict[Any, str] = {}
        # boiler index -> Setpoint. See the setpoint section below.
        self._setpoints: dict[int, Any] = {}
        self._limit_blocks: dict[Any, str] = {}
        self.device_config = None

        # Telemetry pacing lives here rather than in the sensor loop, because a
        # relay change also posts telemetry: both have to agree on when the
        # last post happened or they would double up.
        self._telemetry_lock = asyncio.Lock()
        self._last_telemetry_at: float | None = None

        # Set when a relay changes locally — by the operator, the schedule or a
        # limit cut. The server learns relay state only from telemetry, so
        # without this a local change would wait out the whole interval.
        self.state_changed = asyncio.Event()
        self._state_change_reasons: list[str] = []

        # Database write pacing. Readings are read every ``read_interval`` but
        # only persisted every ``write_interval`` so the sensor loop can react
        # to limits quickly without hammering the SD card.
        self._last_write_at: float | None = None

    async def _wait_for(self, event: asyncio.Event) -> bool:
        """Block until an event fires. Returns False if shutdown came first."""
        if event.is_set():
            return True

        waiters = [
            asyncio.create_task(event.wait()),
            asyncio.create_task(self.shutdown.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()
        return event.is_set()

    async def wait_authenticated(self) -> bool:
        """Block until a session exists. Returns False if shutdown came first."""
        return await self._wait_for(self.authenticated)

    async def wait_mapping_ready(self) -> bool:
        """Block until a device mapping exists. False if shutdown came first."""
        return await self._wait_for(self.mapping_ready)

    async def wait_credentials(self) -> bool:
        """Block until an operator has entered some. False if shutdown first."""
        return await self._wait_for(self.credentials_ready)

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

    async def request_record_refresh(self) -> None:
        """Ask the device-record loop to poll now rather than at its next tick."""
        self.record_refresh_requested.set()

    # -- control mode (automatic = schedule driven, manual = command driven) --

    async def set_mode(self, target: Any, mode: str, *, published: bool = False) -> None:
        """
        Set a unit's control mode.

        ``published`` says the server already knows: true for a mode that
        arrived *as* a command or a peer push, false for one set here, which is
        then reported upstream and retried until acknowledged.
        """
        from mode_store import ModeEntry

        entry = ModeEntry(mode, published)
        async with self._control_lock:
            if self._modes.get(target) == entry:
                return  # no change, no write
            self._modes[target] = entry
            snapshot = dict(self._modes)

        # Persisted here rather than at the call sites so a future caller
        # cannot set a mode that silently fails to survive a restart. The
        # write happens outside the lock: it is file I/O, and nothing else
        # needs to wait for it.
        await self._persist_modes(snapshot)

    async def mark_mode_published(self, target: Any, published: bool = True) -> None:
        """
        Record whether this unit's mode has been reported to the server.

        Set when the report is *sent*, not when it is acknowledged. The server
        does not implement the mode message yet, so waiting for an ack would
        leave every mode permanently unreported — and an unreported mode
        outranks the server's ``desired_mode``, which would block the record
        from ever correcting the device. Sending is the device's whole
        obligation; if the server still disagrees at the next poll, it wins.

        Cleared again when the server explicitly rejects the report, so it is
        offered afresh on the next connection.
        """
        from mode_store import ModeEntry

        async with self._control_lock:
            entry = self._modes.get(target)
            if entry is None or entry.published == published:
                return
            self._modes[target] = ModeEntry(entry.mode, published)
            snapshot = dict(self._modes)

        await self._persist_modes(snapshot)

    async def unpublished_modes(self) -> dict[Any, str]:
        """Modes not yet reported to the server, in a stable order."""
        async with self._control_lock:
            return {
                target: entry.mode
                for target, entry in sorted(self._modes.items(), key=str)
                if not entry.published
            }

    async def restore_modes(self, modes: dict[Any, Any]) -> None:
        """Load modes from the cache at boot, without writing them straight back."""
        async with self._control_lock:
            self._modes.update(modes)

    async def _persist_modes(self, modes: dict[Any, str]) -> None:
        from mode_store import save_modes

        try:
            await save_modes(modes)
        except Exception as exc:
            # A cache that cannot be written must not fail the command that
            # changed the mode; the mode is already in effect.
            await self.log(
                f"[mode] Could not save modes: {exc}", level=logging.WARNING
            )

    async def get_mode(self, target: Any) -> str:
        async with self._control_lock:
            entry = self._modes.get(target)
        return entry.mode if entry is not None else "automatic"

    async def get_modes(self) -> dict[Any, str]:
        """
        Target -> mode name.

        Projected from the stored entries, which also carry whether the server
        has acknowledged each one; callers that only drive relays or build
        telemetry want the mode alone.
        """
        async with self._control_lock:
            return {target: entry.mode for target, entry in self._modes.items()}

    # -- per-boiler temperature setpoints ------------------------------------
    #
    # DEVICE.md gives every boiler its own desired_temperature_c. Where one is
    # set it is what the limit guard holds that boiler at; where it is not, the
    # device-wide max_water_temperature_c still applies. Persisted like modes,
    # and carrying whether the server has acknowledged it, so a setpoint made
    # during an outage is published rather than lost.

    async def set_setpoint(
        self,
        index: int,
        temperature: float,
        *,
        published: bool = False,
    ) -> None:
        from setpoint_store import Setpoint, validate

        entry = Setpoint(validate(temperature), published)
        async with self._control_lock:
            if self._setpoints.get(index) == entry:
                return  # no change, no write
            self._setpoints[index] = entry
            snapshot = dict(self._setpoints)

        await self._persist_setpoints(snapshot)

    async def mark_setpoint_published(self, index: int) -> None:
        """Record that the server has accepted this boiler's setpoint."""
        from setpoint_store import Setpoint

        async with self._control_lock:
            entry = self._setpoints.get(index)
            if entry is None or entry.published:
                return
            self._setpoints[index] = Setpoint(entry.temperature_c, True)
            snapshot = dict(self._setpoints)

        await self._persist_setpoints(snapshot)

    async def clear_setpoint(self, index: int) -> None:
        """Drop a boiler's setpoint, handing it back to the device-wide limit."""
        async with self._control_lock:
            if self._setpoints.pop(index, None) is None:
                return
            snapshot = dict(self._setpoints)

        await self._persist_setpoints(snapshot)

    async def restore_setpoints(self, setpoints: dict[int, Any]) -> None:
        """Load setpoints from the cache at boot, without writing them back."""
        async with self._control_lock:
            self._setpoints.update(setpoints)

    async def _persist_setpoints(self, setpoints: dict[int, Any]) -> None:
        from setpoint_store import save_setpoints

        try:
            await save_setpoints(setpoints)
        except Exception as exc:
            # A cache that cannot be written must not fail the change that set
            # the temperature; the setpoint is already in effect.
            await self.log(
                f"[setpoint] Could not save setpoints: {exc}", level=logging.WARNING
            )

    async def get_setpoint(self, index: int) -> float | None:
        async with self._control_lock:
            entry = self._setpoints.get(index)
        return entry.temperature_c if entry is not None else None

    async def get_setpoints(self) -> dict[int, Any]:
        async with self._control_lock:
            return dict(self._setpoints)

    async def unpublished_setpoints(self) -> dict[int, float]:
        """Setpoints the server has not acknowledged, oldest boiler first."""
        async with self._control_lock:
            return {
                index: entry.temperature_c
                for index, entry in sorted(self._setpoints.items())
                if not entry.published
            }

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

    # -- telemetry pacing ----------------------------------------------------

    async def telemetry_due(self) -> bool:
        """
        Whether the telemetry interval has elapsed since the last post.

        Judged with a little slack, because the check only runs on read-cycle
        boundaries. With the read interval equal to the telemetry interval, a
        cycle landing a second early would fail a strict comparison and defer
        the post to the *next* cycle — turning a one-minute cadence into two.
        """
        interval = await self.get_telemetry_interval()
        slack = min(TELEMETRY_DUE_SLACK_SECONDS, interval * 0.1)
        async with self._telemetry_lock:
            if self._last_telemetry_at is None:
                return True
            return time.monotonic() - self._last_telemetry_at >= interval - slack

    async def mark_telemetry_posted(self) -> None:
        async with self._telemetry_lock:
            self._last_telemetry_at = time.monotonic()

    async def notify_state_change(self, reason: str) -> None:
        """
        Record that a relay changed locally, so it is reported promptly.

        Coalescing is the caller's gain, not its burden: a schedule tick that
        switches four relays calls this four times and produces one post.
        """
        async with self._telemetry_lock:
            self._state_change_reasons.append(reason)
        self.state_changed.set()

    async def take_state_change_reasons(self) -> list[str]:
        async with self._telemetry_lock:
            reasons = list(self._state_change_reasons)
            self._state_change_reasons.clear()
        return reasons

    # -- database write pacing ------------------------------------------------

    async def write_due(self) -> bool:
        """Whether the database write interval has elapsed since the last save."""
        if self._last_write_at is None:
            return True
        return time.monotonic() - self._last_write_at >= self.write_interval

    async def mark_write_done(self) -> None:
        self._last_write_at = time.monotonic()

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

    async def write(self, text: str = "") -> None:
        """
        Write straight to the terminal, creating no log record and never
        captured.

        Frames drawn by the mock display come through here: they are what the
        operator is meant to be reading, so they must not end up in the buffer
        that feeds the panel.
        """
        async with self.print_lock:
            await asyncio.to_thread(print, text)

    async def echo(self, text: str = "") -> None:
        """
        Menu output, to the terminal or to the panel.

        Creates no log record: showing the log file through state.log() would
        append what you are reading back into the file. With a display fitted
        the menu captures these lines and pages them onto it instead of
        printing them, so they are not also duplicated into the journal.
        """
        if self._echo_sink is not None:
            self._echo_sink.extend(str(text).split("\n"))
            return
        await self.write(text)

    # -- echo capture (see _echo_sink) ---------------------------------------

    def capture_echo(self) -> None:
        """Start collecting echo() output instead of printing it."""
        if self._echo_sink is None:
            self._echo_sink = []

    def take_echo(self) -> list[str]:
        """Everything echoed since the last take. Capture stays on."""
        if self._echo_sink is None:
            return []
        lines, self._echo_sink = self._echo_sink, []
        return lines

    def stop_echo_capture(self) -> list[str]:
        """Stop collecting and hand back whatever had not been taken."""
        lines = self.take_echo()
        self._echo_sink = None
        return lines

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
