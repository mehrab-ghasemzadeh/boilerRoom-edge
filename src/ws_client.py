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
from device_config import config_store, parse_config, save_cached_config
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
        "boiler.set_temperature",
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
    superseded = config_store.set_server_document(payload)
    await state.set_device_config(config)
    await state.set_active_config_version(config.version)

    await state.log(
        f"[ws] Applied config v{config.version} "
        f"(telemetry every {config.telemetry_interval_seconds}s, "
        f"limits water {config.limits.min_water_temperature_c}–"
        f"{config.limits.max_water_temperature_c} °C)"
    )

    # A publish outranks limits typed on the device, but never silently: an
    # operator who set a cut-out by hand needs to know it has been replaced.
    if superseded:
        from config_editor import clear_local_config

        try:
            await clear_local_config()
        except Exception as exc:
            await state.log(
                f"[ws] Could not remove the local limits override: {exc}",
                level=logging.WARNING,
            )
        await state.log(
            f"[ws] Published config v{config.version} supersedes {superseded} local "
            "limit edit(s) — the device now runs the published limits",
            level=logging.WARNING,
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


async def _handle_schedule_update_ack(
    message: dict[str, Any],
    state: RuntimeState,
) -> None:
    """
    Outcome of a schedule this device published (DEVICE.md ``schedule.update``).

    On success the cloud has created a schedule version out of what we sent, so
    the local edit is no longer an override — it is the room's schedule, at the
    version named here.

    The state change happens regardless of whether anyone is still waiting on
    the reply: an ack that arrives after the menu gave up waiting must still be
    acted on, or the device would keep flagging a divergence that no longer
    exists.
    """
    payload = message.get("payload") or {}
    correlation_id = str(message.get("correlation_id") or "")
    version = payload.get("schedule_version")

    if payload.get("ok") and isinstance(version, int):
        schedule = await schedule_runner.adopt_published_version(version, state)
        await state.log(
            f"[ws] Schedule published — the server created v{schedule.version} "
            f"from this device's edits ({len(schedule.weekly_rules)} weekly rule(s), "
            f"{len(schedule.exceptions)} exception(s))"
        )
    else:
        await state.log(
            f"[ws] Server rejected schedule.update: {payload.get('error') or payload} "
            "— the edits stay local and will be retried on the next connection",
            level=logging.WARNING,
        )

    future = _pending_acks.get(correlation_id)
    if future is not None and not future.done():
        future.set_result(payload)


async def _handle_set_temperature_ack(
    message: dict[str, Any],
    state: RuntimeState,
) -> None:
    """
    Outcome of a setpoint this device published (``boiler.set_temperature``).

    Acted on whether or not a caller is still waiting: an ack that arrives
    after the menu gave up must still mark the setpoint published, or it would
    be offered again on every reconnect.
    """
    payload = message.get("payload") or {}
    correlation_id = str(message.get("correlation_id") or "")
    index = payload.get("boiler_index")

    if payload.get("ok") and isinstance(index, int):
        await state.mark_setpoint_published(index)
        await state.log(
            f"[ws] Server accepted boiler {index} setpoint "
            f"{payload.get('temperature_c')} °C"
        )
    else:
        await state.log(
            f"[ws] Server rejected boiler.set_temperature: "
            f"{payload.get('error') or payload} — kept locally and retried on "
            "the next connection",
            level=logging.WARNING,
        )

    future = _pending_acks.get(correlation_id)
    if future is not None and not future.done():
        future.set_result(payload)


async def _handle_apply_temperature(
    message: dict[str, Any],
    state: RuntimeState,
) -> None:
    """
    A setpoint another device in this room published (``boiler.apply_temperature``).

    Marked published on arrival: it came *from* the server, so offering it back
    would be a pointless round trip. There is no ack for this message.
    """
    from setpoint_store import SetpointError

    payload = message.get("payload") or {}
    index = payload.get("boiler_index")
    temperature = payload.get("temperature_c")

    if not isinstance(index, int):
        await state.log(
            f"[ws] boiler.apply_temperature without a boiler_index: {payload}",
            level=logging.WARNING,
        )
        return

    try:
        await state.set_setpoint(index, temperature, published=True)
    except SetpointError as exc:
        await state.log(
            f"[ws] boiler.apply_temperature for boiler {index} refused: {exc}",
            level=logging.WARNING,
        )
        return

    await state.log(
        f"[ws] Boiler {index} setpoint set to {float(temperature):.1f} °C "
        "by another device in this room"
    )
    # The cut-out moves with the setpoint, so report the new state promptly.
    await state.notify_state_change(f"boiler {index} setpoint {float(temperature):.1f} °C (peer)")


# Device -> server mode reporting is not in DEVICE.md yet; these names mirror
# boiler.set_temperature / _ack / apply_temperature, which is the shape the
# server is expected to grow. Until it does, the send is simply unanswered and
# the mode stays queued — nothing breaks, and nothing blocks.
MODE_MESSAGE = {"boiler": "boiler.set_mode", "pump": "pump.set_mode"}
MODE_ACK_TYPES = ("boiler.set_mode_ack", "pump.set_mode_ack")
MODE_APPLY_TYPES = ("boiler.apply_mode", "pump.apply_mode")

MODE_INDEX_KEY = {"boiler": "boiler_index", "pump": "pump_index"}


def _mode_target(msg_type: str, payload: dict[str, Any]):
    """The Target a mode message refers to, or None if it names no valid unit."""
    from schedule_runner import Target

    kind = "boiler" if msg_type.startswith("boiler.") else "pump"
    index = payload.get(MODE_INDEX_KEY[kind])
    return Target(kind, index) if isinstance(index, bool) is False and isinstance(index, int) else None


async def _handle_set_mode_ack(
    message: dict[str, Any],
    state: RuntimeState,
) -> None:
    """
    Outcome of a mode this device reported upstream.

    Acted on whether or not a caller is waiting: the send is fire-and-forget,
    so this is the only thing that ever marks a mode published.
    """
    payload = message.get("payload") or {}
    target = _mode_target(message.get("type", ""), payload)

    if payload.get("ok") and target is not None:
        await state.mark_mode_published(target)
        await state.log(f"[ws] Server accepted {target} mode {payload.get('mode')}")
    else:
        # Put it back in the queue: a refused report has not been delivered, so
        # it is offered again rather than left as the server's problem.
        if target is not None:
            await state.mark_mode_published(target, False)
        await state.log(
            f"[ws] Server rejected the mode report: {payload.get('error') or payload} "
            "— kept locally and retried on the next connection",
            level=logging.WARNING,
        )


async def _handle_apply_mode(
    message: dict[str, Any],
    state: RuntimeState,
) -> None:
    """A mode another device in this room set. Marked published on arrival."""
    from mode_store import VALID_MODES

    payload = message.get("payload") or {}
    target = _mode_target(message.get("type", ""), payload)
    mode = str(payload.get("mode") or "").strip().lower()

    if target is None or mode not in VALID_MODES:
        await state.log(
            f"[ws] Unusable mode push: {payload}", level=logging.WARNING
        )
        return

    if await state.get_mode(target) == mode:
        await state.mark_mode_published(target)
        return

    await state.set_mode(target, mode, published=True)

    if mode == "automatic":
        # Hand the unit back to the schedule now rather than at its next
        # start/end boundary, exactly as the command path does.
        schedule_runner.forget(target)
        await schedule_runner.evaluate(state)

    await state.log(f"[ws] {target} set to {mode} by another device in this room")
    await state.notify_state_change(f"{target} mode {mode} (peer)")


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
    elif msg_type == "schedule.update_ack":
        await _handle_schedule_update_ack(message, state)
    elif msg_type == "boiler.set_temperature_ack":
        await _handle_set_temperature_ack(message, state)
    elif msg_type == "boiler.apply_temperature":
        await _handle_apply_temperature(message, state)
    elif msg_type in MODE_ACK_TYPES:
        await _handle_set_mode_ack(message, state)
    elif msg_type in MODE_APPLY_TYPES:
        await _handle_apply_mode(message, state)
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


# The live session, for tasks outside this module that need to push a message
# on it (see push_device_state). None whenever there is no connection.
_active_session: "_Session | None" = None

# Replies we are waiting on, keyed by the event_id we correlated them with.
# Only the listen loop resolves these, so a caller can await an answer without
# reading from the socket itself.
_pending_acks: dict[str, asyncio.Future] = {}

# How long to wait for schedule.update_ack before treating the edit as
# unpublished. It stays queued either way; this only bounds how long an
# operator stares at the menu.
SCHEDULE_UPDATE_ACK_TIMEOUT = 10.0


async def publish_schedule_update(
    state: RuntimeState,
    *,
    timeout: float = SCHEDULE_UPDATE_ACK_TIMEOUT,
) -> dict[str, Any] | None:
    """
    Send the locally edited schedule to the server (DEVICE.md ``schedule.update``).

    Returns the ack payload, or None when there is no session or no reply
    arrived in time — in which case the edit simply stays a local override and
    is retried on the next connection. Nothing is lost either way.
    """
    from schedule_editor import update_payload

    session = _active_session
    document = schedule_runner.document
    if session is None or document is None:
        return None

    message = build_message("schedule.update", update_payload(document))
    event_id = message["event_id"]

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_acks[event_id] = future
    try:
        await session.send_raw(message)
        await state.log(
            f"[ws] Sent schedule.update (event_id={event_id}, "
            f"{len(document.get('weekly_rules') or [])} weekly rule(s))"
        )
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        await state.log(
            f"[ws] No schedule.update_ack within {timeout:.0f}s — "
            "keeping the edits local and retrying on the next connection",
            level=logging.WARNING,
        )
        return None
    except Exception as exc:
        await state.log(f"[ws] schedule.update failed: {exc}", level=logging.WARNING)
        return None
    finally:
        _pending_acks.pop(event_id, None)


async def publish_boiler_temperature(
    state: RuntimeState,
    index: int,
    temperature: float,
    *,
    timeout: float = SCHEDULE_UPDATE_ACK_TIMEOUT,
) -> dict[str, Any] | None:
    """
    Send one boiler's setpoint to the server (DEVICE.md ``boiler.set_temperature``).

    Returns the ack payload, or None when there is no session or no reply
    arrived in time — the setpoint stays unpublished and is retried on the next
    connection. It is already driving the cut-out either way.
    """
    session = _active_session
    if session is None:
        return None

    message = build_message(
        "boiler.set_temperature",
        {"boiler_index": index, "temperature_c": float(temperature)},
    )
    event_id = message["event_id"]

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_acks[event_id] = future
    try:
        await session.send_raw(message)
        await state.log(
            f"[ws] Sent boiler.set_temperature (boiler {index}, "
            f"{float(temperature):.1f} °C, event_id={event_id})"
        )
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        await state.log(
            f"[ws] No boiler.set_temperature_ack for boiler {index} within "
            f"{timeout:.0f}s — retrying on the next connection",
            level=logging.WARNING,
        )
        return None
    except Exception as exc:
        await state.log(
            f"[ws] boiler.set_temperature failed: {exc}", level=logging.WARNING
        )
        return None
    finally:
        _pending_acks.pop(event_id, None)


async def report_unit_mode(state: RuntimeState, target, mode: str) -> bool:
    """
    Report one unit's control mode to the server.

    **Fire and forget.** The server does not implement this message yet, so
    waiting on an ack would stall every mode change for the whole timeout and
    give the operator nothing. The mode is already in force locally and already
    reaches the server as ``reported_mode`` through telemetry; this is what
    lets it also settle ``desired_mode`` once the server grows the message.
    ``_handle_set_mode_ack`` marks it published if and when a reply arrives;
    until then it is offered again on every reconnect.

    Returns whether the message went out, not whether it was accepted.
    """
    session = _active_session
    if session is None:
        return False

    msg_type = MODE_MESSAGE.get(target.type)
    if msg_type is None:
        return False

    try:
        await session.send_message(
            msg_type, {MODE_INDEX_KEY[target.type]: target.index, "mode": mode}
        )
    except Exception as exc:
        await state.log(f"[ws] Could not report {target} mode: {exc}", level=logging.WARNING)
        return False

    # Marked reported on the send, not on an ack. See mark_mode_published:
    # waiting for a reply the server does not yet produce would leave the mode
    # permanently "ours" and stop the device record ever correcting it.
    await state.mark_mode_published(target)
    await state.log(f"[ws] Sent {msg_type} ({target}, {mode})")
    return True


async def _publish_pending_modes(state: RuntimeState) -> None:
    """
    Offer any unacknowledged modes once connected.

    A mode set during an outage is held, not dropped — the same rule the state
    publisher applies to relay changes.
    """
    pending = await state.unpublished_modes()
    if not pending:
        return

    await state.log(
        f"[ws] Reporting {len(pending)} unacknowledged mode(s): "
        + ", ".join(f"{t} {m}" for t, m in pending.items())
    )
    sent = False
    for target, mode in pending.items():
        if state.shutdown.is_set():
            return
        sent |= await report_unit_mode(state, target, mode)

    if sent:
        # The device record is what reconciles modes, and a mode only stops
        # outranking it once reported. Poll now rather than sitting on the
        # wrong modes until the next 15-minute tick.
        await state.request_record_refresh()


async def _publish_pending_setpoints(state: RuntimeState) -> None:
    """
    Offer any unacknowledged setpoints once connected.

    A temperature set during an outage is held, not dropped — the same rule the
    state publisher applies to relay changes and the schedule editor to rules.
    """
    pending = await state.unpublished_setpoints()
    if not pending:
        return

    await state.log(
        f"[ws] Publishing {len(pending)} setpoint(s) set while offline: "
        + ", ".join(f"boiler {i} at {t:.1f} °C" for i, t in pending.items())
    )
    for index, temperature in pending.items():
        if state.shutdown.is_set():
            return
        await publish_boiler_temperature(state, index, temperature)


async def _publish_pending_schedule(state: RuntimeState) -> None:
    """
    Offer any unpublished local schedule to the server once connected.

    A schedule edited while the device was offline is held, not dropped — the
    same rule the state publisher applies to relay changes. Checked after the
    hello handshake, because reconciliation may have pushed a schedule that
    supersedes the edits, in which case there is nothing left to publish.
    """
    if not schedule_runner.is_locally_modified:
        return

    await state.log(
        f"[ws] Publishing {schedule_runner.local_revision} local schedule edit(s) "
        "made while offline"
    )
    await publish_schedule_update(state)


async def push_device_state(state: RuntimeState) -> bool:
    """
    Push ``device.state`` on the open session, if there is one.

    Cheap compared with a telemetry POST — no TLS handshake, which matters on
    the Pi Zero W — so it is what makes a local change show up on a dashboard
    immediately. It does *not* replace telemetry: DEVICE.md defines this
    message as a live dashboard push, and only ``boiler_states`` /
    ``pump_states`` in telemetry update a unit's stored ``reported_state``.
    """
    session = _active_session
    if session is None:
        return False

    from commands import build_state_payload

    try:
        await session.send_message("device.state", await build_state_payload(state))
        return True
    except Exception as exc:
        await state.log(f"[ws] device.state push failed: {exc}", level=logging.DEBUG)
        return False


async def _run_session(state: RuntimeState) -> None:
    global _active_session

    await state.log(f"[ws] Connecting to {_ws_url()} ...")

    ws, _auth_mode = await _connect_ws(state)
    session = _Session(ws)
    _active_session = session
    heartbeat_task: asyncio.Task | None = None
    listen_task: asyncio.Task | None = None
    reconcile_task: asyncio.Task | None = None

    try:
        await state.set_ws_connected(True)
        config_version, schedule_version = await state.get_active_versions()
        last_command_id = await state.get_last_processed_command_id()

        hello_ack = await _perform_hello(
            session,
            state,
            active_config_version=config_version,
            active_schedule_version=schedule_version,
            last_processed_command_id=last_command_id,
        )

        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(session, state), name="ws_heartbeat"
        )
        # Started before reconciliation: the acks it waits on, and the
        # config/schedule the server pushes in answer to hello, all arrive on
        # the listen loop.
        listen_task = asyncio.create_task(
            _listen_loop(session, state), name="ws_listen"
        )

        # Deferred so this module does not depend on the reconciler at import
        # time — it reaches back here for the publish helpers.
        from reconcile import reconcile_after_hello

        # Run alongside the listen loop rather than before it. Reconciliation
        # waits for pushes that the listen loop is what delivers, so awaiting
        # it here would deadlock until the grace period expired.
        reconcile_task = asyncio.create_task(
            reconcile_after_hello(state, hello_ack), name="ws_reconcile"
        )
        await listen_task
    finally:
        _active_session = None
        for task in (heartbeat_task, listen_task, reconcile_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        # Nothing can answer these now, so wake any caller rather than leave it
        # waiting out a timeout on a socket that has already gone. Resolved
        # with None rather than cancelled: cancelling would raise
        # CancelledError into the caller, which is reserved for real shutdown.
        for future in _pending_acks.values():
            if not future.done():
                future.set_result(None)
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
