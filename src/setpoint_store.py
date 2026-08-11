"""
Per-boiler temperature setpoints, persisted across restarts.

DEVICE.md gives every boiler unit its own ``desired_temperature_c``, settable
from four directions:

  * the operator, on the control menu
  * this device, upstream: ``boiler.set_temperature`` -> ``_ack``
  * another device in the same room: ``boiler.apply_temperature``
  * the server, as a ``command.execute`` named ``boiler.set_temperature``

They are per unit by design — boiler 1 can be held at 55 °C for a low-demand
circuit while boiler 2 runs at 75 — which is why a device-wide
``max_water_temperature_c`` cannot express them. Where a boiler has a setpoint,
it is what the limit guard holds that boiler at; where it has none, the
device-wide maximum still applies.

Setpoints are cached like control modes: written whenever one changes, which is
a rare, operator-driven event, and restored before the first sensor cycle so a
boiler comes back after a power cut at the temperature it was left at rather
than at whatever the device-wide default happens to be.

Each entry also records whether the server has acknowledged it. A setpoint set
during an outage is held and published on the next connection, the same rule
the state publisher applies to relay changes.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from json_store import read_json, write_json
from load_env import env_path
from logging_setup import get_logger

_log = get_logger("setpoint")

SETPOINT_CACHE_PATH = env_path("BOILERROOM_SETPOINT_CACHE", "setpoint_cache.json")

# DEVICE.md bounds boiler.set_temperature at 1–80 °C. Anything outside it the
# server would refuse, so it is refused here rather than sent and bounced.
MIN_SETPOINT_C = 1.0
MAX_SETPOINT_C = 80.0


class SetpointError(ValueError):
    """Raised when a setpoint is outside what the platform accepts."""


@dataclass(frozen=True)
class Setpoint:
    """One boiler's target temperature, and whether the server knows about it."""

    temperature_c: float
    published: bool = False


def validate(temperature: Any, *, field: str = "temperature_c") -> float:
    """Bounds-check a setpoint, in °C."""
    try:
        value = float(temperature)
    except (TypeError, ValueError) as exc:
        raise SetpointError(f"{field}: expected a number, got {temperature!r}") from exc

    if not MIN_SETPOINT_C <= value <= MAX_SETPOINT_C:
        raise SetpointError(
            f"{field}: {value:g} °C is outside the "
            f"{MIN_SETPOINT_C:g}–{MAX_SETPOINT_C:g} °C the platform accepts"
        )
    return value


def _parse_entry(raw: Any) -> Setpoint | None:
    """Accept the stored object form, and a bare number from an older cache."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        temperature = raw
        published = True
    elif isinstance(raw, dict):
        temperature = raw.get("temperature_c")
        published = bool(raw.get("published", False))
    else:
        return None

    try:
        return Setpoint(validate(temperature), published)
    except SetpointError:
        return None


async def load_setpoints(path: Path | None = None) -> dict[int, Setpoint]:
    """
    Restore saved setpoints, keyed by boiler index.

    A missing, corrupt or partly invalid cache yields whatever could be read:
    an unreadable file must never stop the agent from starting, and every
    boiler it cannot describe simply falls back to the device-wide limit.
    """
    payload = await read_json(path or SETPOINT_CACHE_PATH)
    if not payload:
        return {}

    raw_setpoints = payload.get("setpoints")
    if not isinstance(raw_setpoints, dict):
        _log.warning("Setpoint cache has no 'setpoints' object — ignoring it")
        return {}

    setpoints: dict[int, Setpoint] = {}
    for raw_index, raw_entry in raw_setpoints.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            _log.warning("Ignoring unusable setpoint cache key %r", raw_index)
            continue

        entry = _parse_entry(raw_entry)
        if entry is None:
            _log.warning("Ignoring unusable setpoint %r for boiler %s", raw_entry, raw_index)
            continue

        setpoints[index] = entry

    return setpoints


async def save_setpoints(
    setpoints: dict[int, Setpoint],
    path: Path | None = None,
) -> None:
    """Persist the current setpoints. Written atomically, like the other caches."""
    payload = {
        "saved_at": datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "setpoints": {
            str(index): {
                "temperature_c": entry.temperature_c,
                "published": entry.published,
            }
            for index, entry in sorted(setpoints.items())
        },
    }
    await write_json(path or SETPOINT_CACHE_PATH, payload)
