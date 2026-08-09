"""
Post sensor readings and device state to the server telemetry API.
"""

from __future__ import annotations

import datetime
import time
import uuid
from typing import Any

from auth import DEVICE_ACCESS_TOKEN, DEVICE_ID, DEVICE_SERIAL, token_manager
from config import GAS_SENSORS, RELAYS, TEMPERATURE_SENSORS

SCHEMA_VERSION = 1

TEMPERATURE_ROLE_TO_API_TYPE = {
    "boiler_input_water": "inlet_temperature",
    "boiler_output_water": "outlet_temperature",
    "boiler_body": "boiler_body_temperature",
    "environment_inside": "ambient_temperature",
    "environment_outside": "ambient_temperature",
}

_start_time = time.monotonic()
_sequence = 0


def _device_id() -> str:
    device_id = DEVICE_ID or DEVICE_SERIAL
    if not device_id:
        raise ValueError(
            "BOILERROOM_DEVICE_ID or BOILERROOM_DEVICE_SERIAL must be set for telemetry"
        )
    return device_id


def _unit_to_boiler_index(unit: str | None) -> int | None:
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

    for sensor_id, value in temperatures.items():
        cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
        role = cfg.get("role", "unknown")
        unit = cfg.get("unit")
        boiler_index = _unit_to_boiler_index(unit)

        entry: dict[str, Any] = {
            "sensor_id": f"temp-{sensor_id}",
            "type": TEMPERATURE_ROLE_TO_API_TYPE.get(role, role),
            "role": role,
            "unit_of_measure": "C",
            "status": "ok" if value is not None else "unavailable",
        }
        if boiler_index is not None:
            entry["boiler_index"] = boiler_index
        if unit:
            entry["equipment_unit"] = unit
        if value is not None:
            entry["value_c"] = value

        readings.append(entry)

    for sensor_id, value in gas.items():
        cfg = GAS_SENSORS.get(sensor_id, {})
        role = cfg.get("role", "unknown")
        unit = cfg.get("unit")
        boiler_index = _unit_to_boiler_index(unit)

        entry: dict[str, Any] = {
            "sensor_id": f"gas-{sensor_id}",
            "type": "gas",
            "role": role,
            "unit_of_measure": "adc",
            "value": value,
            "status": "ok",
        }
        if boiler_index is not None:
            entry["boiler_index"] = boiler_index
        if unit:
            entry["equipment_unit"] = unit

        readings.append(entry)

    return readings


def _build_boiler_states(relay_controller) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []

    for relay_id, cfg in RELAYS.items():
        if cfg.get("role") != "pot":
            continue
        boiler_index = _unit_to_boiler_index(cfg.get("unit"))
        if boiler_index is None:
            continue
        is_on = relay_controller.get_state(relay_id)
        states.append({
            "boiler_index": boiler_index,
            "state": "on" if is_on else "off",
        })

    return states


def _device_status(sensor_readings: list[dict[str, Any]]) -> str:
    if not sensor_readings:
        return "unknown"
    if any(r.get("status") != "ok" for r in sensor_readings):
        return "degraded"
    return "ok"


def build_telemetry_envelope(
    temperatures: dict[int, float | None],
    gas: dict[int, int],
    relay_controller,
) -> dict[str, Any]:
    global _sequence
    _sequence += 1

    sensor_readings = _build_sensor_readings(temperatures, gas)
    captured_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")

    return {
        "schema_version": SCHEMA_VERSION,
        "message_id": str(uuid.uuid4()),
        "device_id": _device_id(),
        "captured_at": captured_at,
        "sequence": _sequence,
        "sensor_readings": sensor_readings,
        "boiler_states": _build_boiler_states(relay_controller),
        "device_status": _device_status(sensor_readings),
        "uptime_seconds": int(time.monotonic() - _start_time),
    }


async def post_telemetry(
    temperatures: dict[int, float | None],
    gas: dict[int, int],
    relay_controller,
) -> dict[str, Any]:
    """POST telemetry envelope to the server. Returns API response."""
    if not DEVICE_ACCESS_TOKEN:
        raise ValueError(
            "BOILERROOM_DEVICE_ACCESS_TOKEN is not set. "
            "Provision the device via POST /api/v1/devices/provision and add the token to .env"
        )

    envelope = build_telemetry_envelope(temperatures, gas, relay_controller)
    path = f"/api/v1/devices/{_device_id()}/telemetry"

    response = await token_manager.request_json_device("POST", path, envelope)
    print(
        f"[telemetry] Posted {len(envelope['sensor_readings'])} readings "
        f"(message_id={envelope['message_id'][:8]}…)"
    )
    return response
