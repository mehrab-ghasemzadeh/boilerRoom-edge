"""
WebSocket client for the device realtime channel.

Handles device.hello / device.hello_ack and keeps the connection open for
future config, schedule, and command messages.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import uuid
from typing import Any
from urllib.parse import quote

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import InvalidStatus

from auth import DEVICE_ACCESS_TOKEN, DEVICE_ID, DEVICE_SERIAL, WS_BASE_URL
from runtime_state import RuntimeState

FIRMWARE_VERSION = os.environ.get("BOILERROOM_FIRMWARE_VERSION", "1.0.0")
HARDWARE_VERSION = os.environ.get("BOILERROOM_HARDWARE_VERSION", "edge-dev")
WS_RECONNECT_DELAY_SECONDS = float(os.environ.get("BOILERROOM_WS_RECONNECT_DELAY", "5"))


def _device_id() -> str:
    device_id = DEVICE_ID or DEVICE_SERIAL
    if not device_id:
        raise ValueError("BOILERROOM_DEVICE_ID must be set for WebSocket")
    return device_id


def _ws_url(*, include_query_token: bool = False) -> str:
    if not DEVICE_ACCESS_TOKEN:
        raise ValueError(
            "BOILERROOM_DEVICE_ACCESS_TOKEN must be set for WebSocket"
        )
    base = WS_BASE_URL.rstrip("/")
    device_id = _device_id()
    url = f"{base}/ws/v1/devices/{device_id}/"
    if include_query_token:
        url = f"{url}?token={quote(DEVICE_ACCESS_TOKEN, safe='')}"
    return url


def _ws_headers(*, include_auth_header: bool = True) -> dict[str, str]:
    if not include_auth_header:
        return {}
    return {"Authorization": f"Device {DEVICE_ACCESS_TOKEN}"}


def _connect_attempts() -> list[tuple[str, str, dict[str, str]]]:
    """Auth strategies ordered by API docs / websocket-mock.json preference."""
    return [
        ("query_token", _ws_url(include_query_token=True), _ws_headers(include_auth_header=False)),
        ("auth_header", _ws_url(include_query_token=False), _ws_headers(include_auth_header=True)),
        ("query_and_header", _ws_url(include_query_token=True), _ws_headers(include_auth_header=True)),
    ]


async def _connect_ws(state: RuntimeState) -> tuple[ClientConnection, str]:
    errors: list[str] = []
    for name, url, headers in _connect_attempts():
        print(headers)
        try:
            ws = await websockets.connect(
                url,
                additional_headers=headers,
                open_timeout=30,
            )
            await state.log(f"[ws] Connected (auth={name})")
            return ws, name
        except InvalidStatus as exc:
            errors.append(f"{name}: HTTP {exc.response.status_code}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    detail = "; ".join(errors)
    raise ConnectionError(f"WebSocket connect failed — {detail}")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def build_message(msg_type: str, payload: dict[str, Any], *, event_id: str | None = None) -> dict[str, Any]:
    return {
        "v": 1,
        "type": msg_type,
        "event_id": event_id or f"evt-{uuid.uuid4().hex[:12]}",
        "sent_at": _utc_now_iso(),
        "payload": payload,
    }


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
            "capabilities": {
                "supports_config_push": True,
                "supports_schedules": True,
            },
        },
    )


async def _send_json(ws: ClientConnection, message: dict[str, Any]) -> None:
    await ws.send(json.dumps(message))


async def _recv_json(ws: ClientConnection) -> dict[str, Any]:
    raw = await ws.recv()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


async def _perform_hello(
    ws: ClientConnection,
    state: RuntimeState,
    *,
    active_config_version: int,
    active_schedule_version: int,
) -> dict[str, Any]:
    hello = build_device_hello(
        active_config_version=active_config_version,
        active_schedule_version=active_schedule_version,
    )
    await _send_json(ws, hello)
    await state.log(
        f"[ws] Sent device.hello (event_id={hello['event_id']}, "
        f"config_v={active_config_version}, schedule_v={active_schedule_version})"
    )

    while True:
        message = await asyncio.wait_for(_recv_json(ws), timeout=30)
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
        await _handle_server_message(message, state)


async def _handle_server_message(message: dict[str, Any], state: RuntimeState) -> None:
    msg_type = message.get("type", "unknown")
    if msg_type in ("config.apply", "schedule.apply", "command.execute"):
        await state.log(f"[ws] Received {msg_type} (handler not implemented yet)")
    elif msg_type != "device.hello_ack":
        await state.log(f"[ws] Received {msg_type}")


async def _listen_loop(ws: ClientConnection, state: RuntimeState) -> None:
    while not state.shutdown.is_set():
        try:
            message = await asyncio.wait_for(_recv_json(ws), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        await _handle_server_message(message, state)


async def _run_session(state: RuntimeState) -> None:
    await state.log(f"[ws] Connecting to {WS_BASE_URL}/ws/v1/devices/{_device_id()}/ ...")

    ws, _auth_mode = await _connect_ws(state)
    try:
        await state.set_ws_connected(True)
        config_v, schedule_v = await state.get_active_versions()
        await _perform_hello(ws, state, active_config_version=config_v, active_schedule_version=schedule_v)
        await _listen_loop(ws, state)
    finally:
        await ws.close()
        await state.set_ws_connected(False)


async def run_websocket_client(state: RuntimeState) -> None:
    """Connect, perform hello handshake, reconnect on failure until shutdown."""
    if not DEVICE_ACCESS_TOKEN or not (DEVICE_ID or DEVICE_SERIAL):
        await state.log(
            "[ws] Skipped — set BOILERROOM_DEVICE_ID and BOILERROOM_DEVICE_ACCESS_TOKEN"
        )
        return

    while not state.shutdown.is_set():
        try:
            await _run_session(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await state.set_ws_connected(False)
            await state.log(f"[ws] Connection error: {exc}")

        if state.shutdown.is_set():
            break

        await state.log(f"[ws] Reconnecting in {WS_RECONNECT_DELAY_SECONDS:.0f}s ...")
        try:
            await asyncio.wait_for(
                state.shutdown.wait(),
                timeout=WS_RECONNECT_DELAY_SECONDS,
            )
        except asyncio.TimeoutError:
            pass

    await state.set_ws_connected(False)


