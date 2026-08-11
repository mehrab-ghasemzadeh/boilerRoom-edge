"""
Build a device mapping from the server's device record.

The record now carries the wiring the agent needs — ``physical_id`` and
``channel`` on sensors, ``gpio`` and ``role`` on relays, and the boiler/pump
unit each is attached to — so the platform can be the single source of truth
for the installation, with no mapping file to maintain by hand on each
device.

This module converts a ``DeviceRecord`` into the mapping document shape, so it
goes through exactly the same validation as any other mapping. Anything
the record cannot answer is reported as a ``RecordMappingError`` naming the
field, rather than being guessed: a mapping is what drives relays and what the
over-temperature cut reads, and a plausible-looking guess there is worse than a
refusal.
"""

from __future__ import annotations

import re
from typing import Any

from device_record import DeviceRecord, ServerRelay, ServerSensor
from logging_setup import get_logger
from mapping_schema import (
    GAS_ROLES,
    RELAY_ROLES,
    TEMPERATURE_ROLES,
    TEMPERATURE_ROLES_REQUIRING_UNIT,
)

# Equipment units are addressed as pot_<index> throughout the agent: the
# schedule, the limit guard and the command handlers all build that string from
# a boiler or pump index.
UNIT_PREFIX = "pot_"

_log = get_logger("mapping")

_KEY_SUFFIX = re.compile(r"(\d+)$")


class RecordMappingError(ValueError):
    """Raised when the server record cannot produce a usable mapping."""


def _unit_id(index: int | None) -> str | None:
    return f"{UNIT_PREFIX}{index}" if index is not None else None


def _index_by_id(record: DeviceRecord) -> tuple[dict[int, int], dict[int, int]]:
    """
    ``id`` -> ``index`` for boilers and pumps.

    Sensors and relays reference a unit by its primary key, which is *not* its
    index: this installation has a pump with ``id=4, index=2``. Reading the
    reference as an index put its relay on a boiler that does not exist, so
    commands for pump 2 found nothing to switch.
    """
    return (
        {u.unit_id: u.index for u in record.boiler_units if u.unit_id is not None},
        {u.unit_id: u.index for u in record.pump_units if u.unit_id is not None},
    )


def _local_key(config_key: str, sequence: int | None, fallback: int) -> int:
    """
    Local handle for a sensor or relay.

    The server's ``config_key``/``relay_key`` (``ts_1``, ``gs_1``, ``rly_1``)
    carries a stable per-kind number; use it so local ids stay put across
    fetches. Sequence, then position, are the fallbacks.
    """
    match = _KEY_SUFFIX.search(config_key or "")
    if match:
        return int(match.group(1))
    if sequence is not None:
        return sequence
    return fallback


def _temperature_entry(sensor: ServerSensor, boilers: dict[int, int]) -> dict[str, Any]:
    if not sensor.physical_id:
        raise RecordMappingError(
            f"sensor {sensor.sensor_uid!r}: no physical_id — the 1-Wire address is "
            "needed to read the probe"
        )
    return {
        "physical_id": sensor.physical_id,
        "role": sensor.type,
        "unit": _unit_id(boilers.get(sensor.boiler_unit)),
        "sensor_id": sensor.sensor_uid,
    }


def _gas_entry(sensor: ServerSensor, boilers: dict[int, int]) -> dict[str, Any]:
    if sensor.channel is None:
        raise RecordMappingError(
            f"sensor {sensor.sensor_uid!r}: no channel — the ADC channel is needed "
            "to read the sensor"
        )
    return {
        "channel": sensor.channel,
        "role": sensor.type,
        "unit": _unit_id(boilers.get(sensor.boiler_unit)),
        "sensor_id": sensor.sensor_uid,
    }


def _relay_entry(
    relay: ServerRelay,
    boilers: dict[int, int],
    pumps: dict[int, int],
) -> dict[str, Any]:
    if relay.gpio is None:
        raise RecordMappingError(
            f"relay {relay.relay_key or '?'}: no gpio — the pin is needed to drive it"
        )
    if relay.role not in RELAY_ROLES:
        raise RecordMappingError(
            f"relay {relay.relay_key or '?'}: unknown role {relay.role!r}. "
            f"Valid roles: {sorted(RELAY_ROLES)}"
        )

    # A relay belongs to a boiler or to a pump, never both. Both resolve to the
    # same pot_<n> unit id, which is how the schedule addresses them.
    if relay.boiler_unit is not None and relay.pump_unit is not None:
        raise RecordMappingError(
            f"relay {relay.relay_key or '?'}: attached to both boiler "
            f"{relay.boiler_unit} and pump {relay.pump_unit}"
        )

    if relay.boiler_unit is not None:
        index = boilers.get(relay.boiler_unit)
        if index is None:
            raise RecordMappingError(
                f"relay {relay.relay_key or '?'}: boiler_unit {relay.boiler_unit} "
                "does not match any registered boiler"
            )
    elif relay.pump_unit is not None:
        index = pumps.get(relay.pump_unit)
        if index is None:
            raise RecordMappingError(
                f"relay {relay.relay_key or '?'}: pump_unit {relay.pump_unit} "
                "does not match any registered pump"
            )
    else:
        index = None  # site-wide relay: alarm, light

    return {
        "gpio": relay.gpio,
        "role": relay.role,
        "unit": _unit_id(index),
    }


def mapping_from_record(record: DeviceRecord) -> dict[str, Any]:
    """
    Convert a device record into a raw mapping document.

    The result is validated by ``parse_mapping`` like any other mapping, so
    role names, unit references and required fields are checked there.
    """
    units: dict[str, dict[str, str]] = {}
    for unit in record.boiler_units + record.pump_units:
        unit_id = _unit_id(unit.index)
        # Boilers are added first, so a boiler's name wins over a pump sharing
        # the index — they are two roles on one pot, not two pots.
        if unit_id and unit_id not in units:
            units[unit_id] = {"name": unit.name or unit_id}

    boilers, pumps = _index_by_id(record)

    temperature_sensors: dict[str, dict[str, Any]] = {}
    gas_sensors: dict[str, dict[str, Any]] = {}

    for position, sensor in enumerate(record.sensors, start=1):
        if not sensor.enabled:
            # A disabled probe is not wired up as far as the platform is
            # concerned; reading it would only produce faults.
            continue

        key = str(_local_key(sensor.config_key, sensor.sequence, position))

        if sensor.type in TEMPERATURE_ROLES:
            if key in temperature_sensors:
                raise RecordMappingError(
                    f"sensor {sensor.sensor_uid!r}: duplicate local key {key!r}"
                )
            temperature_sensors[key] = _temperature_entry(sensor, boilers)
        elif sensor.type in GAS_ROLES:
            if key in gas_sensors:
                raise RecordMappingError(
                    f"sensor {sensor.sensor_uid!r}: duplicate local key {key!r}"
                )
            gas_sensors[key] = _gas_entry(sensor, boilers)
        else:
            raise RecordMappingError(
                f"sensor {sensor.sensor_uid!r}: unknown type {sensor.type!r}. "
                f"Temperature roles: {sorted(TEMPERATURE_ROLES)}; "
                f"gas roles: {sorted(GAS_ROLES)}"
            )

    relays: dict[str, dict[str, Any]] = {}
    for position, relay in enumerate(record.relays, start=1):
        if not relay.enabled:
            continue
        key = str(_local_key(relay.relay_key, None, position))
        if key in relays:
            raise RecordMappingError(
                f"relay {relay.relay_key or '?'}: duplicate local key {key!r}"
            )
        relays[key] = _relay_entry(relay, boilers, pumps)

    # Every unit reference is resolved through the declared units above, so
    # anything left dangling here is a bug in this module rather than in the
    # record. Previously this was papered over by inventing the missing unit,
    # which is exactly what hid a relay being attached to the wrong pump.
    referenced = {
        entry["unit"]
        for entry in (*temperature_sensors.values(), *gas_sensors.values(), *relays.values())
        if entry.get("unit")
    }
    missing = sorted(referenced - set(units))
    if missing:
        raise RecordMappingError(
            f"internal: unit(s) {missing} were referenced but never declared"
        )

    return {
        "units": units,
        "temperature_sensors": temperature_sensors,
        "gas_sensors": gas_sensors,
        "relays": relays,
    }


def _signatures(document: dict[str, Any]) -> dict[str, dict[str, tuple]]:
    """Comparable form of a mapping document, keyed by section and local id."""
    return {
        "temperature sensor": {
            key: (entry.get("physical_id"), entry.get("role"), entry.get("unit"))
            for key, entry in document.get("temperature_sensors", {}).items()
        },
        "gas sensor": {
            key: (entry.get("channel"), entry.get("role"), entry.get("unit"))
            for key, entry in document.get("gas_sensors", {}).items()
        },
        "relay": {
            key: (entry.get("gpio"), entry.get("role"), entry.get("unit"))
            for key, entry in document.get("relays", {}).items()
        },
    }


def mapping_differences(document: dict[str, Any]) -> list[str]:
    """
    How a candidate mapping differs from the one currently driving the hardware.

    Used to tell an operator that a change made on the server has not taken
    effect yet, without swapping wiring underneath a running relay controller.
    """
    from config import GAS_SENSORS, RELAYS, TEMPERATURE_SENSORS

    active = {
        "temperature sensor": {
            str(key): (cfg.get("physical_id"), cfg.get("role"), cfg.get("unit"))
            for key, cfg in TEMPERATURE_SENSORS.items()
        },
        "gas sensor": {
            str(key): (cfg.get("channel"), cfg.get("role"), cfg.get("unit"))
            for key, cfg in GAS_SENSORS.items()
        },
        "relay": {
            str(key): (cfg.get("gpio"), cfg.get("role"), cfg.get("unit"))
            for key, cfg in RELAYS.items()
        },
    }

    candidate = _signatures(document)
    lines: list[str] = []

    for section, wanted in candidate.items():
        running = active.get(section, {})
        for key in sorted(set(wanted) | set(running), key=lambda k: (len(k), k)):
            if key not in running:
                lines.append(f"{section} {key} added on the server: {wanted[key]}")
            elif key not in wanted:
                lines.append(f"{section} {key} removed on the server (running: {running[key]})")
            elif wanted[key] != running[key]:
                lines.append(
                    f"{section} {key} changed: running {running[key]}, server {wanted[key]}"
                )

    if lines:
        lines.append("restart the service to adopt the server's wiring")

    return lines


def readiness_problems(record: DeviceRecord) -> list[str]:
    """
    Why this record cannot yet drive the installation.

    Reported before the mapping is adopted so an incomplete record produces one
    actionable list rather than a single exception about whichever field
    happened to be checked first.
    """
    problems: list[str] = []

    if not record.sensors:
        problems.append("no sensors are registered on the server")
    if not record.relays:
        problems.append(
            "no relays are registered on the server — nothing could be switched"
        )

    for sensor in record.sensors:
        if not sensor.enabled:
            continue
        if sensor.type in TEMPERATURE_ROLES and not sensor.physical_id:
            problems.append(f"sensor {sensor.sensor_uid}: physical_id is empty")
        if sensor.type in GAS_ROLES and sensor.channel is None:
            problems.append(f"sensor {sensor.sensor_uid}: channel is empty")

    # The over-temperature cut finds a boiler's probes by unit. A water probe
    # the cut cannot tie to a boiler leaves that boiler unprotected, so an
    # unset *or unresolvable* boiler_unit is worth refusing over.
    boilers, _ = _index_by_id(record)
    for sensor in record.sensors:
        if not sensor.enabled:
            continue
        if sensor.type not in TEMPERATURE_ROLES_REQUIRING_UNIT:
            continue
        if sensor.boiler_unit is None:
            problems.append(
                f"sensor {sensor.sensor_uid} ({sensor.type}): no boiler_unit — the "
                "over-temperature cut could not associate it with a boiler"
            )
        elif sensor.boiler_unit not in boilers:
            problems.append(
                f"sensor {sensor.sensor_uid} ({sensor.type}): boiler_unit "
                f"{sensor.boiler_unit} does not match any registered boiler"
            )

    return problems
