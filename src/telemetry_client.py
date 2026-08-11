"""
Post sensor readings and device state to the server telemetry API.

The envelope follows the schema in DEVICE.md. Fields the device cannot
determine (no wifi interface, for example) are omitted rather than sent as
nulls.

Errors are deliberately *not* included in the envelope: they go to
POST /devices/<id>/errors, where each entry is documented to become an alert.
Repeating them here risks duplicate alerts, and an empty ``errors: []`` on a
device that has just reported a fault would be actively misleading.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
from typing import Any

from auth import device_id as _device_id
from auth import token_manager
from config import GAS_SENSORS, RELAYS, TEMPERATURE_SENSORS
from data_logger import OUTBOX_MAX_ATTEMPTS, reading_store
from device_record import device_record_store
from mapping_schema import api_sensor_id
from schedule_runner import Target
from system_metrics import read_system_metrics, uptime_seconds

SCHEMA_VERSION = 1

# How many queued envelopes to retry per send. Each is a separate TLS
# handshake on one ARMv6 core, so the backlog drains steadily rather than
# monopolising a cycle: 10 a minute clears a day's outage in about two hours.
FLUSH_BATCH = 10

# Serialises every send, so a backlog replay can never overtake a live post.
_send_lock = asyncio.Lock()

TEMPERATURE_ROLE_TO_API_TYPE = {
    "boiler_input_water": "inlet_temperature",
    "boiler_output_water": "outlet_temperature",
    "boiler_body": "boiler_body_temperature",
    "environment_inside": "ambient_temperature",
    "environment_outside": "ambient_temperature",
}

# DEVICE.md's telemetry example uses "normal" for a healthy device. The server
# stores whatever it is given without validating, so the documented vocabulary
# is the only guide.
STATUS_NORMAL = "normal"
STATUS_DEGRADED = "degraded"
STATUS_UNKNOWN = "unknown"

_sequence = 0


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _unit_to_index(unit: str | None) -> int | None:
    if not unit or not unit.startswith("pot_"):
        return None
    try:
        return int(unit.split("_", 1)[1])
    except (IndexError, ValueError):
        return None


def _build_sensor_readings(
    temperatures: dict[int, float | None],
    gas: dict[int, int],
) -> list[dict[str, Any]]:
    readings: list[dict[str, Any]] = []
    disabled = device_record_store.disabled

    for sensor_id, value in temperatures.items():
        cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
        role = cfg.get("role", "unknown")
        unit = cfg.get("unit")
        boiler_index = _unit_to_index(unit)

        api_id = api_sensor_id("temp", sensor_id, cfg)
        # A probe the operator disabled server-side is not reported. It keeps
        # being read and stored locally, and the limit guard still sees it —
        # disabling a probe must not quietly remove it from the safety cut.
        if api_id in disabled:
            continue

        entry: dict[str, Any] = {
            "sensor_id": api_id,
            "type": TEMPERATURE_ROLE_TO_API_TYPE.get(role, role),
            "unit": "C",
            "status": "ok" if value is not None else "unavailable",
            # Extra context so the server can correlate a reading with the
            # physical installation; ignored by the documented schema.
            "role": role,
        }
        if value is not None:
            entry["value"] = value
        if boiler_index is not None:
            entry["boiler_index"] = boiler_index
        if unit:
            entry["equipment_unit"] = unit

        readings.append(entry)

    for sensor_id, value in gas.items():
        cfg = GAS_SENSORS.get(sensor_id, {})
        role = cfg.get("role", "unknown")
        unit = cfg.get("unit")
        boiler_index = _unit_to_index(unit)

        api_id = api_sensor_id("gas", sensor_id, cfg)
        if api_id in disabled:
            continue

        entry = {
            "sensor_id": api_id,
            "type": "gas",
            "value": value,
            "unit": "adc",
            "status": "ok",
            "role": role,
        }
        if boiler_index is not None:
            entry["boiler_index"] = boiler_index
        if unit:
            entry["equipment_unit"] = unit

        readings.append(entry)

    return readings


def _build_unit_states(
    relay_controller,
    *,
    relay_role: str,
    index_key: str,
    target_type: str,
    modes: dict[Any, str],
    setpoints: dict[int, Any] | None = None,
    temperatures: dict[int, float | None] | None = None,
) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []

    for relay_id, cfg in sorted(RELAYS.items()):
        if cfg.get("role") != relay_role:
            continue
        index = _unit_to_index(cfg.get("unit"))
        if index is None:
            continue

        entry: dict[str, Any] = {
            index_key: index,
            "state": "on" if relay_controller.get_state(relay_id) else "off",
            "mode": modes.get(Target(target_type, index), "automatic"),
        }

        # DEVICE.md: setpoint_c updates the unit's desired_temperature_c, and
        # temperature_c its reported_temperature_c. Both are optional and are
        # sent only where the device actually has the number, since a missing
        # probe is not a reading of zero.
        setpoint = (setpoints or {}).get(index)
        if setpoint is not None:
            entry["setpoint_c"] = setpoint.temperature_c

        if temperatures is not None:
            measured = _unit_temperature(cfg.get("unit"), temperatures)
            if measured is not None:
                entry["temperature_c"] = round(measured, 2)

        states.append(entry)

    return states


def _unit_temperature(
    unit: str | None,
    temperatures: dict[int, float | None],
) -> float | None:
    """
    The temperature to report for a unit: its control probe.

    The same probe the limit guard judges recovery on — outlet, else body, else
    inlet — so the number the server stores as ``reported_temperature_c`` is
    the one the cut-out is actually working from.
    """
    if not unit:
        return None

    from limits_guard import _control_temperature

    return _control_temperature(unit, temperatures)


def _device_status(sensor_readings: list[dict[str, Any]]) -> str:
    if not sensor_readings:
        return STATUS_UNKNOWN
    if any(reading.get("status") != "ok" for reading in sensor_readings):
        return STATUS_DEGRADED
    return STATUS_NORMAL


async def build_telemetry_envelope(
    state,
    temperatures: dict[int, float | None],
    gas: dict[int, int],
) -> dict[str, Any]:
    global _sequence
    _sequence += 1

    relay_controller = state.relay_controller
    modes = await state.get_modes()
    setpoints = await state.get_setpoints()
    config_version, schedule_version = await state.get_active_versions()
    metrics = await read_system_metrics()

    sensor_readings = _build_sensor_readings(temperatures, gas)

    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "message_id": str(uuid.uuid4()),
        "device_id": _device_id(),
        "captured_at": _utc_now_iso(),
        "sequence": _sequence,
        "device_status": _device_status(sensor_readings),
        "active_config_version": config_version,
        "active_schedule_version": schedule_version,
        "uptime_seconds": uptime_seconds(),
        "sensor_readings": sensor_readings,
    }

    if relay_controller is not None:
        envelope["boiler_states"] = _build_unit_states(
            relay_controller,
            relay_role="pot",
            index_key="boiler_index",
            target_type="boiler",
            modes=modes,
            setpoints=setpoints,
            temperatures=temperatures,
        )
        envelope["pump_states"] = _build_unit_states(
            relay_controller,
            relay_role="pump",
            index_key="pump_index",
            target_type="pump",
            modes=modes,
        )

    network: dict[str, Any] = {}
    if metrics.get("network_type"):
        network["type"] = metrics["network_type"]
    if metrics.get("rssi_dbm") is not None:
        network["rssi_dbm"] = metrics["rssi_dbm"]
    if network:
        envelope["network"] = network

    return envelope


async def _send(envelope: dict[str, Any]) -> dict[str, Any]:
    path = f"/api/v1/devices/{_device_id()}/telemetry"
    return await token_manager.request_json("POST", path, envelope)


class TelemetryQueued(RuntimeError):
    """The envelope was stored for retry instead of being delivered."""


async def flush_outbox(state, limit: int = FLUSH_BATCH) -> tuple[int, bool]:
    """
    Re-send undelivered telemetry, oldest first.

    Returns ``(delivered, drained)``, where ``drained`` means the queue is now
    empty — the caller must not send anything newer until it is.

    Stops at the first failure: if one post cannot reach the server, the next
    almost certainly cannot either, and a Pi Zero W should not spend its cycle
    on a queue of doomed TLS handshakes.

    Order matters beyond tidiness. ``boiler_states`` / ``pump_states`` set the
    server's ``reported_state``, so a backlog entry delivered *after* the live
    post would leave the cloud showing a relay position from minutes ago. The
    caller holds ``_send_lock`` across the flush and its own post to keep the
    sequence strictly chronological.
    """
    pending = await reading_store.pending_telemetry(limit)
    if not pending:
        return 0, True

    sent = 0
    blocked = False
    for row_id, captured_at, raw in pending:
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Unparseable rows would block the queue forever otherwise.
            await reading_store.record_telemetry_attempt(row_id, f"corrupt: {exc}")
            continue

        try:
            await _send(envelope)
        except Exception as exc:
            attempts = await reading_store.record_telemetry_attempt(row_id, str(exc))
            if attempts >= OUTBOX_MAX_ATTEMPTS:
                await state.log(
                    f"[telemetry] Giving up on the backlog entry from {captured_at} "
                    f"after {attempts} attempts: {exc}",
                    level=logging.WARNING,
                )
            blocked = True
            break

        await reading_store.mark_telemetry_synced(row_id)
        sent += 1

    if sent:
        await state.log(
            f"[telemetry] Delivered {sent} queued envelope(s) from the outbox"
        )

    # A full batch means there may be more behind it; either way the queue is
    # not known to be empty, so nothing newer may go ahead of it.
    return sent, not blocked and len(pending) < limit


async def post_telemetry(
    state,
    temperatures: dict[int, float | None],
    gas: dict[int, int],
) -> dict[str, Any]:
    """
    POST a telemetry envelope. Returns the API response.

    A failed post is not lost: the envelope goes to the outbox and is retried
    with its original ``captured_at``, so the server ends up with the readings
    as they were taken rather than a gap.
    """
    envelope = await build_telemetry_envelope(state, temperatures, gas)

    async with _send_lock:
        # Anything queued goes first, so the live post is the last word on
        # relay state.
        _, drained = await flush_outbox(state)

        if not drained:
            # Sending now would put this envelope ahead of older ones still in
            # the queue. They carry boiler_states / pump_states too, so
            # delivering them afterwards would rewind the server's view of the
            # relays. Queue this one and keep the order intact.
            await reading_store.enqueue_telemetry(
                envelope["captured_at"], json.dumps(envelope), "queued behind backlog"
            )
            raise TelemetryQueued(
                "backlog not drained — envelope queued to preserve ordering"
            )

        try:
            response = await _send(envelope)
        except Exception as exc:
            await reading_store.enqueue_telemetry(
                envelope["captured_at"], json.dumps(envelope), str(exc)
            )
            await state.log(
                f"[telemetry] Post failed, queued for retry "
                f"(captured_at={envelope['captured_at']}): {exc}",
                level=logging.WARNING,
            )
            raise

    await state.log(
        f"[telemetry] Posted {len(envelope['sensor_readings'])} readings "
        f"(seq={envelope['sequence']}, status={envelope['device_status']})"
    )
    return response
