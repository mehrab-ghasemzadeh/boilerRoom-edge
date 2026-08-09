"""
WebSocket client for the device realtime channel.

Implements the device side of the protocol in DEVICE.md:

  device -> server   device.hello, device.heartbeat, command.ack,
                     command.result, config.result, schedule.result,
                     device.state
  server -> device   device.hello_ack, config.apply, schedule.apply,
                     command.execute

Sends are serialised through _Session because the heartbeat task and the
message handlers both write to the same connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import json
import os
import time
import uuid
from typing import Any
from urllib.parse import quote

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import InvalidStatus

from auth import WS_BASE_URL, device_id, token_manager
from commands import command_executor
from device_config import parse_config, save_cached_config
from runtime_state import RuntimeState
from schedule_runner import save_cached_schedule, schedule_runner
from system_metrics import read_system_metrics

FIRMWARE_VERSION = os.environ.get("BOILERROOM_FIRMWARE_VERSION", "1.0.0")
HARDWARE_VERSION = os.environ.get("BOILERROOM_HARDWARE_VERSION", "edge-dev")
WS_RECONNECT_DELAY_SECONDS = float(os.environ.get("BOILERROOM_WS_RECONNECT_DELAY", "5"))

# Reconnects are backed off exponentially up to this ceiling. Each attempt costs
# a full TLS handshake, which is expensive on the Pi Zero W's single ARMv6 core,
# so a server that drops us immediately must not turn into a reconnect storm.
WS_RECONNECT_MAX_DELAY_SECONDS = float(
    os.environ.get("BOILERROOM_WS_RECONNECT_MAX_DELAY", "60")
)

# A session that survives this long is treated as healthy, resetting the backoff.
WS_SESSION_STABLE_SECONDS = float(
    os.environ.get("BOILERROOM_WS_SESSION_STABLE", "30")
)

CAPABILITIES = {
    "supports_config_push": True,
    "supports_schedules": True,
    "supported_commands": [
        "boiler.turn_on",
        "boiler.turn_off",
        "boiler.set_mode",
        "pump.turn_on",
        "pump.turn_off",
        "pump.set_mode",
        "device.request_state",
        "device.restart_service",
        "alarm.acknowledge_local",
    ],
}


def _ws_origin() -> str:
    """
    Origin header for the handshake.

    The server runs Channels' AllowedHostsOriginValidator, which rejects a
    handshake with no Origin header (HTTP 403) before auth is ever checked.
    Non-browser clients send no Origin by default, so derive one from the WS
    base URL: wss://host -> https://host.
    """
    override = os.environ.get("BOILERROOM_WS_ORIGIN")
    if override:
        return override
    base = WS_BASE_URL.rstrip("/")
    if base.startswith("wss://"):
        return "https://" + base[len("wss://"):]
    if base.startswith("ws://"):
        return "http://" + base[len("ws://"):]
    return base


def _ws_url(token: str | None = None) -> str:
    """Device channel URL; DEVICE.md accepts the token as ?token= or a header."""
    base = WS_BASE_URL.rstrip("/")
    url = f"{base}/ws/v1/devices/{device_id()}/"
    if token:
        url = f"{url}?token={quote(token, safe='')}"
    return url


def _connect_attempts(token: str) -> list[tuple[str, str, dict[str, str]]]:
    """Both auth forms documented in DEVICE.md, query token first."""
    origin = {"Origin": _ws_origin()}
    return [
        ("query_token", _ws_url(token), origin),
        ("auth_header", _ws_url(), {**origin, "Authorization": f"Device {token}"}),
    ]


async def _connect_ws(state: RuntimeState) -> tuple[ClientConnection, str]:
    token = await token_manager.ensure_valid_token()
    errors: list[str] = []
    for name, url, headers in _connect_attempts(token):
        try:
            ws = await websockets.connect(
                url,
                additional_headers=headers,
                open_timeout=30,
            )
            await state.log(f"[ws] Connected (auth={name})")
            return ws, name
        except InvalidStatus as exc:
            status = exc.response.status_code
            hint = ""
            if status == 403:
                hint = f" (rejected Origin {_ws_origin()!r}, or bad token / device_id)"
            errors.append(f"{name}: HTTP {status}{hint}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    detail = "; ".join(errors)
    raise ConnectionError(f"WebSocket connect failed — {detail}")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def build_message(
    msg_type: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    message = {
        "v": 1,
        "type": msg_type,
        "event_id": event_id or f"evt-{uuid.uuid4().hex[:12]}",
        "sent_at": _utc_now_iso(),
        "payload": payload,
    }
    if correlation_id:
        message["correlation_id"] = correlation_id
    return message


class _Session:
    """One connection. Serialises sends so concurrent tasks cannot interleave."""

    def __init__(self, ws: ClientConnection):
        self.ws = ws
        self._send_lock = asyncio.Lock()

    async def send_raw(self, message: dict[str, Any]) -> dict[str, Any]:
        async with self._send_lock:
            await self.ws.send(json.dumps(message))
        return message

    async def send_message(
        self,
        msg_type: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.send_raw(
            build_message(msg_type, payload, correlation_id=correlation_id)
        )

    async def recv_json(self) -> dict[str, Any]:
        raw = await self.ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def close(self) -> None:
        await self.ws.close()


def build_device_hello(
    *,
    active_config_version: int = 0,
    active_schedule_version: int = 0,
    last_processed_command_id: str | None = None,
) -> dict[str, Any]:
    return build_message(
        "device.hello",
        {
            "firmware_version": FIRMWARE_VERSION,
            "hardware_version": HARDWARE_VERSION,
            "active_config_version": active_config_version,
            "active_schedule_version": active_schedule_version,
            "last_processed_command_id": last_processed_command_id,
            "capabilities": CAPABILITIES,
        },
    )


async def _perform_hello(
    session: _Session,
    state: RuntimeState,
    *,
    active_config_version: int,
    active_schedule_version: int,
    last_processed_command_id: str | None,
) -> dict[str, Any]:
    hello = build_device_hello(
        active_config_version=active_config_version,
        active_schedule_version=active_schedule_version,
        last_processed_command_id=last_processed_command_id,
    )
    await session.send_raw(hello)
    await state.log(
        f"[ws] Sent device.hello (event_id={hello['event_id']}, "
        f"config_v={active_config_version}, schedule_v={active_schedule_version})"
    )

    while True:
        message = await asyncio.wait_for(session.recv_json(), timeout=30)
        msg_type = message.get("type", "")

        if msg_type == "device.hello_ack":
            await state.set_hello_ack(message)
            payload = message.get("payload", {})
            await state.log(
                "[ws] Received device.hello_ack — "
                f"server_time={payload.get('server_time')}, "
                f"desired_config_v={payload.get('desired_config_version')}, "
                f"desired_schedule_v={payload.get('desired_schedule_version')}, "
                f"heartbeat_interval={payload.get('heartbeat_interval_seconds')}s"
            )
            return message

        await state.log(f"[ws] Message during hello handshake: {msg_type}")
        await _handle_server_message(session, message, state)


# ---------------------------------------------------------------------------
# Server -> device handlers
# ---------------------------------------------------------------------------


async def _handle_config_apply(
    session: _Session,
    message: dict[str, Any],
    state: RuntimeState,
) -> None:
    payload = message.get("payload") or {}
    correlation_id = message.get("event_id")
    raw_version = payload.get("config_version")

    try:
        config = parse_config(payload)
    except Exception as exc:
        await state.log(f"[ws] config.apply failed: {exc}", level=logging.ERROR)
        await session.send_message(
            "config.result",
            {
                "config_version": raw_version if isinstance(raw_version, int) else 0,
                "status": "failed",
                "error": str(exc),
            },
            correlation_id=correlation_id,
        )
        return

    previous = await state.get_device_config()
    await state.set_device_config(config)
    await state.set_active_config_version(config.version)

    await state.log(
        f"[ws] Applied config v{config.version} "
        f"(telemetry every {config.telemetry_interval_seconds}s, "
        f"limits water {config.limits.min_water_temperature_c}–"
        f"{config.limits.max_water_temperature_c} °C)"
    )

    # Only touch the SD card when the version actually moved.
    if previous is None or previous.version != config.version:
        try:
            await save_cached_config(payload)
            await state.log(f"[ws] Cached config v{config.version} to disk")
        except Exception as exc:
            await state.log(f"[ws] Could not cache config: {exc}", level=logging.WARNING)

    await session.send_message(
        "config.result",
        {"config_version": config.version, "status": "applied"},
        correlation_id=correlation_id,
    )
    await state.log(f"[ws] Sent config.result (v{config.version}, applied)")


async def _handle_schedule_apply(
    session: _Session,
    message: dict[str, Any],
    state: RuntimeState,
) -> None:
    """Apply a pushed schedule to the relays and report the outcome."""
    payload = message.get("payload") or {}
    correlation_id = message.get("event_id")
    raw_version = payload.get("schedule_version")

    previous = schedule_runner.schedule

    try:
        schedule = await schedule_runner.apply_document(payload, state)
    except Exception as exc:
        await state.log(f"[ws] schedule.apply failed: {exc}", level=logging.ERROR)
        await session.send_message(
            "schedule.result",
            {
                "schedule_version": raw_version if isinstance(raw_version, int) else 0,
                "status": "failed",
                "error": str(exc),
            },
            correlation_id=correlation_id,
        )
        return

    # Only touch the SD card when the version actually moved.
    if previous is None or previous.version != schedule.version:
        try:
            await save_cached_schedule(payload)
            await state.log(f"[ws] Cached schedule v{schedule.version} to disk")
        except Exception as exc:
            await state.log(f"[ws] Could not cache schedule: {exc}", level=logging.WARNING)

    await session.send_message(
        "schedule.result",
        {"schedule_version": schedule.version, "status": "applied"},
        correlation_id=correlation_id,
    )
    await state.log(f"[ws] Sent schedule.result (v{schedule.version}, applied)")


async def _handle_command_execute(
    session: _Session,
    message: dict[str, Any],
    state: RuntimeState,
) -> None:
    payload = message.get("payload") or {}
    correlation_id = message.get("event_id")
    command_id = str(payload.get("command_id") or "")
    name = str(payload.get("name") or "")

    if not command_id or not name:
        await state.log("[ws] command.execute missing command_id/name — rejected")
        await session.send_message(
            "command.ack",
            {"command_id": command_id, "accepted": False, "stage": "received"},
            correlation_id=correlation_id,
        )
        await session.send_message(
            "command.result",
            {
                "command_id": command_id,
                "status": "failed",
                "error": "command_id and name are required",
            },
            correlation_id=correlation_id,
        )
        return

    # The server re-dispatches queued/sent commands after every hello, so a
    # repeat must replay the stored outcome instead of acting twice.
    previous = command_executor.previous_outcome(command_id)
    if previous is not None:
        await state.log(f"[ws] command.execute {name} ({command_id}) already handled — replaying")
        await session.send_message(
            "command.ack",
            {"command_id": command_id, "accepted": previous.accepted, "stage": "received"},
            correlation_id=correlation_id,
        )
        await session.send_message(
            "command.result",
            previous.result_payload(command_id),
            correlation_id=correlation_id,
        )
        return

    await state.log(f"[ws] Received command.execute {name} ({command_id})")
    await session.send_message(
        "command.ack",
        {"command_id": command_id, "accepted": True, "stage": "received"},
        correlation_id=correlation_id,
    )

    outcome = await command_executor.execute(
        payload,
        state,
        session,
        expires_at=message.get("expires_at"),
    )

    await session.send_message(
        "command.result",
        outcome.result_payload(command_id),
        correlation_id=correlation_id,
    )
    await state.log(
        f"[ws] Sent command.result ({command_id}, {outcome.status}"
        f"{f': {outcome.error}' if outcome.error else ''})"
    )


async def _handle_server_message(
    session: _Session,
    message: dict[str, Any],
    state: RuntimeState,
) -> None:
    msg_type = message.get("type", "unknown")

    if msg_type == "schedule.apply":
        await _handle_schedule_apply(session, message, state)
    elif msg_type == "config.apply":
        await _handle_config_apply(session, message, state)
    elif msg_type == "command.execute":
        await _handle_command_execute(session, message, state)
    elif msg_type != "device.hello_ack":
        await state.log(f"[ws] Received {msg_type}")


# ---------------------------------------------------------------------------
# Session tasks
# ---------------------------------------------------------------------------


async def _heartbeat_loop(session: _Session, state: RuntimeState) -> None:
    """Keepalive at the interval the server asked for in hello_ack."""
    while not state.shutdown.is_set():
        interval = await state.get_heartbeat_interval()
        try:
            await asyncio.wait_for(state.shutdown.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass

        metrics = await read_system_metrics()
        config_version, schedule_version = await state.get_active_versions()

        try:
            await session.send_message(
                "device.heartbeat",
                {
                    **metrics,
                    "active_config_version": config_version,
                    "active_schedule_version": schedule_version,
                },
            )
        except Exception as exc:
            await state.log(f"[ws] Heartbeat failed: {exc}", level=logging.WARNING)
            return

        await state.log(
            f"[ws] Sent device.heartbeat (uptime={metrics.get('uptime_seconds')}s)"
        )


async def _listen_loop(session: _Session, state: RuntimeState) -> None:
    while not state.shutdown.is_set():
        if state.ws_reconnect_requested.is_set():
            state.ws_reconnect_requested.clear()
            await state.log("[ws] Reconnect requested — closing session")
            return

        try:
            message = await asyncio.wait_for(session.recv_json(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        await _handle_server_message(session, message, state)


async def _run_session(state: RuntimeState) -> None:
    await state.log(f"[ws] Connecting to {_ws_url()} ...")

    ws, _auth_mode = await _connect_ws(state)
    session = _Session(ws)
    heartbeat_task: asyncio.Task | None = None

    try:
        await state.set_ws_connected(True)
        config_version, schedule_version = await state.get_active_versions()
        last_command_id = await state.get_last_processed_command_id()

        await _perform_hello(
            session,
            state,
            active_config_version=config_version,
            active_schedule_version=schedule_version,
            last_processed_command_id=last_command_id,
        )

        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(session, state), name="ws_heartbeat"
        )
        await _listen_loop(session, state)
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task
        await session.close()
        await state.set_ws_connected(False)


async def run_websocket_client(state: RuntimeState) -> None:
    """Connect, perform hello handshake, reconnect on failure until shutdown."""
    if not token_manager.is_authenticated:
        await state.log("[ws] Waiting for a device session before connecting ...")
    if not await state.wait_authenticated():
        return

    delay = WS_RECONNECT_DELAY_SECONDS

    while not state.shutdown.is_set():
        started = time.monotonic()
        try:
            await _run_session(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await state.set_ws_connected(False)
            await state.log(f"[ws] Connection error: {exc}", level=logging.WARNING)

        if state.shutdown.is_set():
            break

        if time.monotonic() - started >= WS_SESSION_STABLE_SECONDS:
            delay = WS_RECONNECT_DELAY_SECONDS
        else:
            delay = min(delay * 2, WS_RECONNECT_MAX_DELAY_SECONDS)

        await state.log(f"[ws] Reconnecting in {delay:.0f}s ...")
        try:
            await asyncio.wait_for(state.shutdown.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    await state.set_ws_connected(False)
