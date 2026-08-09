"""
Mapping schema, validation, and lookup helpers.

Raw mapping documents (from mapping.json or a future server API) are parsed
into a DeviceMapping via parse_mapping().
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Temperature sensor roles (4 kinds)
# ---------------------------------------------------------------------------

TEMPERATURE_ROLES = frozenset({
    "environment_inside",
    "environment_outside",
    "boiler_input_water",
    "boiler_output_water",
    "boiler_body",
})

TEMPERATURE_ROLES_REQUIRING_UNIT = frozenset({
    "boiler_input_water",
    "boiler_output_water",
    "boiler_body",
})

TEMPERATURE_ROLE_LABELS = {
    "environment_inside": "inside environment",
    "environment_outside": "outside environment",
    "boiler_input_water": "input water",
    "boiler_output_water": "output water",
    "boiler_body": "boiler body",
}

# ---------------------------------------------------------------------------
# Relay roles
# ---------------------------------------------------------------------------

RELAY_ROLES = frozenset({
    "pot",
    "pump",
    "circulation_pump",
    "torch",
    "fan",
    "gas_valve",
    "mixer",
    "alarm",
    "backup_heater",
    "light",
})

RELAY_ROLE_LABELS = {
    "pot": "pot",
    "pump": "pump",
    "circulation_pump": "circulation pump",
    "torch": "torch",
    "fan": "fan",
    "gas_valve": "gas valve",
    "mixer": "mixer",
    "alarm": "alarm",
    "backup_heater": "backup heater",
    "light": "light",
}

# ---------------------------------------------------------------------------
# Gas sensor roles (optional unit)
# ---------------------------------------------------------------------------

GAS_ROLES = frozenset({
    "boiler_room",
    "gas_valve",
    "exhaust",
})

GAS_ROLE_LABELS = {
    "boiler_room": "boiler room",
    "gas_valve": "gas valve",
    "exhaust": "exhaust",
}


@dataclass(frozen=True)
class DeviceMapping:
    units: dict[str, dict[str, str]]
    temperature_sensors: dict[int, dict[str, Any]]
    gas_sensors: dict[int, dict[str, Any]]
    relays: dict[int, dict[str, Any]]


def _unit_name(units: dict[str, dict[str, str]], unit_id: str | None) -> str | None:
    if not unit_id:
        return None
    return units.get(unit_id, {}).get("name", unit_id)


def _build_label(
    role: str,
    role_labels: dict[str, str],
    units: dict[str, dict[str, str]],
    unit_id: str | None,
) -> str:
    role_label = role_labels.get(role, role.replace("_", " "))
    unit_label = _unit_name(units, unit_id)
    if unit_label:
        return f"{unit_label} – {role_label}"
    return role_label.capitalize()


def _parse_int_keys(section: dict[str, Any], section_name: str) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for key, value in section.items():
        try:
            sensor_id = int(key)
        except ValueError as exc:
            raise ValueError(
                f"{section_name}: keys must be numeric strings, got {key!r}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(f"{section_name}[{key}]: expected an object")
        result[sensor_id] = value
    return result


def _validate_temperature_sensors(
    sensors: dict[int, dict[str, Any]],
    units: dict[str, dict[str, str]],
) -> dict[int, dict[str, Any]]:
    validated: dict[int, dict[str, Any]] = {}

    for sensor_id, entry in sensors.items():
        physical_id = entry.get("physical_id")
        role = entry.get("role")
        unit = entry.get("unit")

        if not physical_id:
            raise ValueError(
                f"temperature_sensors[{sensor_id}]: 'physical_id' is required"
            )
        if role not in TEMPERATURE_ROLES:
            raise ValueError(
                f"temperature_sensors[{sensor_id}]: unknown role {role!r}. "
                f"Valid roles: {sorted(TEMPERATURE_ROLES)}"
            )
        if role in TEMPERATURE_ROLES_REQUIRING_UNIT and not unit:
            raise ValueError(
                f"temperature_sensors[{sensor_id}]: role {role!r} requires 'unit'"
            )
        if unit and unit not in units:
            raise ValueError(
                f"temperature_sensors[{sensor_id}]: unknown unit {unit!r}"
            )

        validated[sensor_id] = {
            "physical_id": physical_id,
            "role": role,
            "unit": unit,
            "name": _build_label(role, TEMPERATURE_ROLE_LABELS, units, unit),
        }

    return validated


def _validate_gas_sensors(
    sensors: dict[int, dict[str, Any]],
    units: dict[str, dict[str, str]],
) -> dict[int, dict[str, Any]]:
    validated: dict[int, dict[str, Any]] = {}

    for sensor_id, entry in sensors.items():
        channel = entry.get("channel")
        role = entry.get("role")
        unit = entry.get("unit")

        if channel is None:
            raise ValueError(f"gas_sensors[{sensor_id}]: 'channel' is required")
        if role not in GAS_ROLES:
            raise ValueError(
                f"gas_sensors[{sensor_id}]: unknown role {role!r}. "
                f"Valid roles: {sorted(GAS_ROLES)}"
            )
        if unit and unit not in units:
            raise ValueError(f"gas_sensors[{sensor_id}]: unknown unit {unit!r}")

        validated[sensor_id] = {
            "channel": channel,
            "role": role,
            "unit": unit,
            "name": _build_label(role, GAS_ROLE_LABELS, units, unit),
        }

    return validated


def _validate_relays(
    relays: dict[int, dict[str, Any]],
    units: dict[str, dict[str, str]],
) -> dict[int, dict[str, Any]]:
    validated: dict[int, dict[str, Any]] = {}

    for relay_id, entry in relays.items():
        gpio = entry.get("gpio")
        role = entry.get("role")
        unit = entry.get("unit")

        if gpio is None:
            raise ValueError(f"relays[{relay_id}]: 'gpio' is required")
        if role not in RELAY_ROLES:
            raise ValueError(
                f"relays[{relay_id}]: unknown role {role!r}. "
                f"Valid roles: {sorted(RELAY_ROLES)}"
            )
        if unit and unit not in units:
            raise ValueError(f"relays[{relay_id}]: unknown unit {unit!r}")

        validated[relay_id] = {
            "gpio": gpio,
            "role": role,
            "unit": unit,
            "name": _build_label(role, RELAY_ROLE_LABELS, units, unit),
        }

    return validated


def parse_mapping(raw: dict[str, Any]) -> DeviceMapping:
    """Validate and normalize a raw mapping document."""
    units = raw.get("units", {})
    if not isinstance(units, dict):
        raise ValueError("'units' must be an object")

    temperature_sensors = _validate_temperature_sensors(
        _parse_int_keys(raw.get("temperature_sensors", {}), "temperature_sensors"),
        units,
    )
    gas_sensors = _validate_gas_sensors(
        _parse_int_keys(raw.get("gas_sensors", {}), "gas_sensors"),
        units,
    )
    relays = _validate_relays(
        _parse_int_keys(raw.get("relays", {}), "relays"),
        units,
    )

    return DeviceMapping(
        units=units,
        temperature_sensors=temperature_sensors,
        gas_sensors=gas_sensors,
        relays=relays,
    )


def find_temperature_sensor(mapping: DeviceMapping, unit: str, role: str) -> int | None:
    for sensor_id, cfg in mapping.temperature_sensors.items():
        if cfg["role"] == role and cfg.get("unit") == unit:
            return sensor_id
    return None


def find_relay(mapping: DeviceMapping, unit: str, role: str) -> int | None:
    for relay_id, cfg in mapping.relays.items():
        if cfg["role"] == role and cfg.get("unit") == unit:
            return relay_id
    return None


def temperature_sensors_for_unit(mapping: DeviceMapping, unit: str) -> dict[str, int]:
    return {
        cfg["role"]: sensor_id
        for sensor_id, cfg in mapping.temperature_sensors.items()
        if cfg.get("unit") == unit
    }


def relays_for_unit(mapping: DeviceMapping, unit: str) -> dict[str, int]:
    return {
        cfg["role"]: relay_id
        for relay_id, cfg in mapping.relays.items()
        if cfg.get("unit") == unit
    }
