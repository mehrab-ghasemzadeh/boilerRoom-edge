"""
Device self-detail (GET /api/v1/devices/<id>).

A device token may fetch its own record, and that record is the only place the
server tells us things it never pushes over the WebSocket:

* ``sensors[]`` — the ``sensor_uid`` the platform knows each probe by, whether
  it is still ``enabled``, and a ``calibration.offset_c`` to apply to readings.
* ``boiler_units[]`` / ``pump_units[]`` — the cloud's ``desired_state`` and
  ``desired_mode`` next to the ``reported_*`` values it last heard from us.
* ``capabilities`` — what the server believes this device can do, which is our
  own hello echoed back.

The record also carries the wiring — ``physical_id``, ``channel``, ``gpio``
and the boiler/pump each belongs to — so it is the device's only mapping
source; see ``record_mapping.py`` for the conversion.

The last successful fetch is cached to disk so calibration and enable flags
survive a reboot with no network.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import TEMPERATURE_SENSORS
from json_store import read_json, write_json
from load_env import env_path
from logging_setup import get_logger
from mapping_schema import api_sensor_id

_log = get_logger("device")

DEVICE_RECORD_CACHE_PATH = env_path(
    "BOILERROOM_DEVICE_RECORD_CACHE", "device_record_cache.json"
)


class DeviceRecordError(ValueError):
    """Raised when a device record cannot be parsed."""


@dataclass(frozen=True)
class ServerSensor:
    """A sensor as the platform has it on file."""

    sensor_uid: str
    type: str = ""
    sequence: int | None = None
    unit: str = ""
    enabled: bool = True
    offset_c: float = 0.0
    label: str = ""
    latest_value: float | None = None
    latest_status: str = ""
    latest_captured_at: str = ""
    # Wiring: what makes the record usable as a mapping source.
    config_key: str = ""
    physical_id: str = ""
    channel: int | None = None
    boiler_unit: int | None = None


@dataclass(frozen=True)
class ServerRelay:
    """A relay as the platform has it on file."""

    relay_key: str = ""
    gpio: int | None = None
    role: str = ""
    boiler_unit: int | None = None
    pump_unit: int | None = None
    enabled: bool = True


@dataclass(frozen=True)
class ServerUnit:
    """A boiler or pump as the platform has it on file."""

    kind: str  # "boiler" or "pump"
    index: int
    # The platform's primary key. Sensors and relays reference a unit by this,
    # *not* by index — the two differ (a pump can be id 4, index 2).
    unit_id: int | None = None
    name: str = ""
    enabled: bool = True
    desired_state: str = ""
    reported_state: str = ""
    desired_mode: str = ""
    reported_mode: str = ""
    # Per-unit setpoint, DEVICE.md's boiler.set_temperature target. Sent as a
    # string by the platform, so it is parsed rather than trusted as a number.
    desired_temperature_c: float | None = None
    reported_temperature_c: float | None = None

    @property
    def state_diverged(self) -> bool:
        # "unknown" means the server has never heard a state for this unit, not
        # that it disagrees with one.
        return (
            bool(self.desired_state)
            and bool(self.reported_state)
            and self.reported_state != "unknown"
            and self.desired_state != self.reported_state
        )

    @property
    def mode_diverged(self) -> bool:
        return (
            bool(self.desired_mode)
            and bool(self.reported_mode)
            and self.desired_mode != self.reported_mode
        )


@dataclass(frozen=True)
class DeviceRecord:
    public_id: str
    serial_number: str = ""
    boiler_room: Any = None
    status: str = ""
    presence: str = ""
    device_status: str = ""
    firmware_version: str = ""
    hardware_version: str = ""
    active_config_version: int | None = None
    active_schedule_version: int | None = None
    supported_commands: tuple[str, ...] = ()
    supports_schedules: bool = False
    supports_config_push: bool = False
    paired_at: str = ""
    sensors: tuple[ServerSensor, ...] = ()
    relays: tuple[ServerRelay, ...] = ()
    boiler_units: tuple[ServerUnit, ...] = ()
    pump_units: tuple[ServerUnit, ...] = ()
    fetched_at: str = ""

    @property
    def units(self) -> tuple[ServerUnit, ...]:
        return self.boiler_units + self.pump_units

    def calibration_offsets(self) -> dict[str, float]:
        """``sensor_uid`` -> offset in °C, skipping the zero offsets."""
        return {s.sensor_uid: s.offset_c for s in self.sensors if s.offset_c}

    def disabled_sensor_uids(self) -> frozenset[str]:
        return frozenset(s.sensor_uid for s in self.sensors if not s.enabled)


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _optional_int(raw: Any, field_name: str) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise DeviceRecordError(f"{field_name}: expected an integer, got {raw!r}") from exc


def _optional_float(raw: Any, field_name: str) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise DeviceRecordError(f"{field_name}: expected a number, got {raw!r}") from exc


def _parse_sensor(entry: Any, position: int) -> ServerSensor | None:
    where = f"sensors[{position}]"
    if not isinstance(entry, dict):
        raise DeviceRecordError(f"{where}: expected an object")

    uid = entry.get("sensor_uid")
    if not isinstance(uid, str) or not uid.strip():
        # A sensor with no uid cannot be matched to a local probe, so it is of
        # no use to us. Skipping beats failing the whole record.
        _log.warning("%s: no sensor_uid — ignoring this entry", where)
        return None

    calibration = entry.get("calibration") or {}
    if not isinstance(calibration, dict):
        raise DeviceRecordError(f"{where}.calibration: expected an object")

    return ServerSensor(
        sensor_uid=uid.strip(),
        type=str(entry.get("type") or ""),
        sequence=_optional_int(entry.get("sequence"), f"{where}.sequence"),
        unit=str(entry.get("unit") or ""),
        enabled=bool(entry.get("enabled", True)),
        offset_c=_optional_float(calibration.get("offset_c"), f"{where}.calibration.offset_c") or 0.0,
        label=str(calibration.get("label") or ""),
        latest_value=_optional_float(entry.get("latest_value"), f"{where}.latest_value"),
        latest_status=str(entry.get("latest_status") or ""),
        latest_captured_at=str(entry.get("latest_captured_at") or ""),
        config_key=str(entry.get("config_key") or ""),
        physical_id=str(entry.get("physical_id") or ""),
        channel=_optional_int(entry.get("channel"), f"{where}.channel"),
        boiler_unit=_optional_int(entry.get("boiler_unit"), f"{where}.boiler_unit"),
    )


def _parse_relays(raw: Any) -> tuple[ServerRelay, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DeviceRecordError("'relays' must be a list")

    relays: list[ServerRelay] = []
    for position, entry in enumerate(raw):
        where = f"relays[{position}]"
        if not isinstance(entry, dict):
            raise DeviceRecordError(f"{where}: expected an object")

        relays.append(
            ServerRelay(
                relay_key=str(entry.get("relay_key") or ""),
                gpio=_optional_int(entry.get("gpio"), f"{where}.gpio"),
                role=str(entry.get("role") or ""),
                boiler_unit=_optional_int(entry.get("boiler_unit"), f"{where}.boiler_unit"),
                pump_unit=_optional_int(entry.get("pump_unit"), f"{where}.pump_unit"),
                enabled=bool(entry.get("enabled", True)),
            )
        )

    return tuple(relays)


def _parse_units(raw: Any, kind: str) -> tuple[ServerUnit, ...]:
    field_name = f"{kind}_units"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DeviceRecordError(f"{field_name}: expected a list")

    units: list[ServerUnit] = []
    for position, entry in enumerate(raw):
        where = f"{field_name}[{position}]"
        if not isinstance(entry, dict):
            raise DeviceRecordError(f"{where}: expected an object")

        index = _optional_int(entry.get("index"), f"{where}.index")
        if index is None:
            _log.warning("%s: no index — ignoring this entry", where)
            continue

        units.append(
            ServerUnit(
                kind=kind,
                index=index,
                unit_id=_optional_int(entry.get("id"), f"{where}.id"),
                name=str(entry.get("name") or ""),
                enabled=bool(entry.get("enabled", True)),
                desired_state=str(entry.get("desired_state") or ""),
                reported_state=str(entry.get("reported_state") or ""),
                desired_mode=str(entry.get("desired_mode") or ""),
                reported_mode=str(entry.get("reported_mode") or ""),
                desired_temperature_c=_optional_float(
                    entry.get("desired_temperature_c"), f"{where}.desired_temperature_c"
                ),
                reported_temperature_c=_optional_float(
                    entry.get("reported_temperature_c"), f"{where}.reported_temperature_c"
                ),
            )
        )

    return tuple(units)


def parse_device_record(payload: dict[str, Any]) -> DeviceRecord:
    """Validate and normalise a device detail payload (the ``data`` object)."""
    if not isinstance(payload, dict):
        raise DeviceRecordError("device record must be an object")

    public_id = payload.get("public_id")
    if not isinstance(public_id, str) or not public_id.strip():
        raise DeviceRecordError(f"'public_id' must be a non-empty string, got {public_id!r}")

    raw_sensors = payload.get("sensors")
    if raw_sensors is not None and not isinstance(raw_sensors, list):
        raise DeviceRecordError("'sensors' must be a list")

    sensors: list[ServerSensor] = []
    for position, entry in enumerate(raw_sensors or []):
        sensor = _parse_sensor(entry, position)
        if sensor is not None:
            sensors.append(sensor)

    capabilities = payload.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        raise DeviceRecordError("'capabilities' must be an object")

    raw_commands = capabilities.get("supported_commands") or []
    if not isinstance(raw_commands, list):
        raise DeviceRecordError("capabilities.supported_commands: expected a list")

    return DeviceRecord(
        public_id=public_id.strip(),
        serial_number=str(payload.get("serial_number") or ""),
        boiler_room=payload.get("boiler_room"),
        status=str(payload.get("status") or ""),
        presence=str(payload.get("presence") or ""),
        device_status=str(payload.get("device_status") or ""),
        firmware_version=str(payload.get("firmware_version") or ""),
        hardware_version=str(payload.get("hardware_version") or ""),
        active_config_version=_optional_int(
            payload.get("active_config_version"), "active_config_version"
        ),
        active_schedule_version=_optional_int(
            payload.get("active_schedule_version"), "active_schedule_version"
        ),
        supported_commands=tuple(str(c) for c in raw_commands),
        supports_schedules=bool(capabilities.get("supports_schedules", False)),
        supports_config_push=bool(capabilities.get("supports_config_push", False)),
        paired_at=str(payload.get("paired_at") or ""),
        sensors=tuple(sensors),
        relays=_parse_relays(payload.get("relays")),
        boiler_units=_parse_units(payload.get("boiler_units"), "boiler"),
        pump_units=_parse_units(payload.get("pump_units"), "pump"),
        fetched_at=_utc_now_iso(),
    )


# ---------------------------------------------------------------------------
# Fetch and cache
# ---------------------------------------------------------------------------
#
# As with the config and schedule caches, the raw payload is stored rather than
# the parsed dataclass, so a reload runs the same validation as a live fetch.


async def fetch_device_record() -> tuple[DeviceRecord, dict[str, Any]]:
    """
    GET the device's own record. Returns ``(record, raw_payload)``.

    Requires a device session; the caller is expected to wait for one.
    """
    from auth import device_id, token_manager

    path = f"/api/v1/devices/{device_id()}"
    response = await token_manager.request_json("GET", path)

    payload = response.get("data") if isinstance(response, dict) else None
    if not isinstance(payload, dict):
        raise DeviceRecordError("response has no 'data' object")

    return parse_device_record(payload), payload


async def load_cached_device_record(
    path: Path | None = None,
) -> tuple[DeviceRecord | None, str | None]:
    """
    Load the last fetched record from disk.

    Returns ``(record, error)``. A missing cache yields ``(None, None)`` — that
    is an ordinary first boot, not a fault.
    """
    payload = await read_json(path or DEVICE_RECORD_CACHE_PATH)
    if payload is None:
        return None, None

    try:
        return parse_device_record(payload), None
    except DeviceRecordError as exc:
        return None, str(exc)


async def save_cached_device_record(
    payload: dict[str, Any],
    path: Path | None = None,
) -> None:
    await write_json(path or DEVICE_RECORD_CACHE_PATH, payload)


# ---------------------------------------------------------------------------
# Active record
# ---------------------------------------------------------------------------


class DeviceRecordStore:
    """
    Holds the active record and the lookups derived from it.

    ``offsets`` and ``disabled`` are read on every sensor cycle, so they are
    kept as plain attributes replaced wholesale rather than rebuilt or locked
    per read: rebinding is atomic under the GIL and there is one event loop.
    """

    def __init__(self) -> None:
        self._record: DeviceRecord | None = None
        self._offsets: dict[str, float] = {}
        self._disabled: frozenset[str] = frozenset()

    @property
    def record(self) -> DeviceRecord | None:
        return self._record

    @property
    def offsets(self) -> dict[str, float]:
        return self._offsets

    @property
    def disabled(self) -> frozenset[str]:
        return self._disabled

    def set_record(self, record: DeviceRecord) -> None:
        self._record = record
        self._offsets = record.calibration_offsets()
        self._disabled = record.disabled_sensor_uids()

    def offset_for(self, sensor_uid: str) -> float:
        return self._offsets.get(sensor_uid, 0.0)

    def is_disabled(self, sensor_uid: str) -> bool:
        return sensor_uid in self._disabled


device_record_store = DeviceRecordStore()


def apply_calibration(
    temperatures: dict[int, float | None],
) -> dict[int, float | None]:
    """
    Add each probe's server-side ``offset_c`` to its reading.

    Applied at the top of the sensor cycle so the corrected value is what gets
    stored locally, what the limit guard judges, and what telemetry reports —
    a calibration that only reached the upload would leave the safety cut
    working from numbers the operator never sees.

    Probes with no offset are returned untouched, and when the server has sent
    no offsets at all the original dict comes straight back: this runs on every
    cycle on a single-core Pi.
    """
    offsets = device_record_store.offsets
    if not offsets:
        return temperatures

    adjusted: dict[int, float | None] = {}
    for sensor_id, value in temperatures.items():
        if value is None:
            adjusted[sensor_id] = None
            continue
        uid = api_sensor_id("temp", sensor_id, TEMPERATURE_SENSORS.get(sensor_id, {}))
        offset = offsets.get(uid)
        adjusted[sensor_id] = round(value + offset, 2) if offset else value

    return adjusted


# ---------------------------------------------------------------------------
# Reconciliation reporting
# ---------------------------------------------------------------------------


def divergences(record: DeviceRecord) -> list[str]:
    """
    Units where the cloud's intent differs from what we last reported.

    Deliberately reported, not acted on. The server re-dispatches queued
    commands after every hello, so acting on ``desired_state`` here would race
    that flow and could silently flip a boiler to manual — taking it off the
    schedule — without a command ever being issued.
    """
    lines: list[str] = []
    for unit in record.units:
        label = f"{unit.kind} {unit.index}"
        if unit.state_diverged:
            lines.append(
                f"{label}: server wants state={unit.desired_state}, "
                f"we last reported {unit.reported_state}"
            )
        if unit.mode_diverged:
            lines.append(
                f"{label}: server wants mode={unit.desired_mode}, "
                f"we last reported {unit.reported_mode}"
            )
    return lines


def capability_mismatches(record: DeviceRecord, declared: dict[str, Any]) -> list[str]:
    """
    Compare the server's view of our capabilities with what hello declared.

    A mismatch means a hello never landed, or the server is holding an older
    view — worth knowing, since it decides which commands it will queue.
    """
    lines: list[str] = []

    ours = set(declared.get("supported_commands") or ())
    theirs = set(record.supported_commands)
    missing = sorted(ours - theirs)
    extra = sorted(theirs - ours)

    if missing:
        lines.append(f"server does not know we support: {', '.join(missing)}")
    if extra:
        lines.append(f"server expects commands we do not implement: {', '.join(extra)}")

    for flag in ("supports_schedules", "supports_config_push"):
        if bool(declared.get(flag)) != bool(getattr(record, flag)):
            lines.append(
                f"{flag}: we declare {bool(declared.get(flag))}, "
                f"server has {bool(getattr(record, flag))}"
            )

    return lines


def describe(record: DeviceRecord | None) -> list[str]:
    """Human-readable summary for the control menu."""
    if record is None:
        return ["No device record fetched yet."]

    lines = [
        f"Device {record.public_id}"
        + (f" (serial {record.serial_number})" if record.serial_number else ""),
        f"  Status:     {record.status or '—'} / presence {record.presence or '—'}",
        f"  Room:       {record.boiler_room}",
        f"  Versions:   config v{record.active_config_version}, "
        f"schedule v{record.active_schedule_version} (server's view)",
        f"  Fetched:    {record.fetched_at or '—'}",
    ]

    lines.append(f"  Sensors ({len(record.sensors)}):")
    for sensor in record.sensors:
        flags = []
        if not sensor.enabled:
            flags.append("DISABLED")
        if sensor.offset_c:
            flags.append(f"offset {sensor.offset_c:+.2f} °C")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        latest = (
            f"last {sensor.latest_value} @ {sensor.latest_captured_at}"
            if sensor.latest_value is not None
            else "no reading on file"
        )
        lines.append(f"    {sensor.sensor_uid:<12} {sensor.type:<24} {latest}{suffix}")

    for kind, units in (("Boilers", record.boiler_units), ("Pumps", record.pump_units)):
        lines.append(f"  {kind} ({len(units)}):")
        for unit in units:
            lines.append(
                f"    {unit.kind} {unit.index}: {unit.name or '—'} "
                f"desired {unit.desired_state}/{unit.desired_mode}, "
                f"reported {unit.reported_state}/{unit.reported_mode}"
                + ("" if unit.enabled else "  [DISABLED]")
            )

    diverged = divergences(record)
    if diverged:
        lines.append("  Divergences (reported, not applied):")
        lines.extend(f"    {line}" for line in diverged)

    return lines
