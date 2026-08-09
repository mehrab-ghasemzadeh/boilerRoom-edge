"""
config.apply document (see DEVICE.md).

The server pushes an operating configuration: safety limits, telemetry cadence,
and the boiler/sensor inventory it believes this device has. This module parses
and holds it; the device replies config.result with applied/failed.

Note: the limits are stored and exposed, but nothing enforces them yet — a
boiler is not cut when water exceeds max_water_temperature_c. That belongs to
the control logic, not the transport layer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from load_env import env_path
from json_store import read_json, write_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The last applied config is cached here so a restart without network still has
# its limits and telemetry cadence, and so the server does not have to re-push
# an unchanged document on every boot.
CONFIG_CACHE_PATH = env_path("BOILERROOM_CONFIG_CACHE", "config_cache.json")


class ConfigError(ValueError):
    """Raised when a config document cannot be parsed."""


@dataclass(frozen=True)
class Limits:
    max_water_temperature_c: float | None = None
    min_water_temperature_c: float | None = None
    max_ambient_temperature_c: float | None = None


@dataclass(frozen=True)
class DeviceConfig:
    version: int
    site_label: str = ""
    limits: Limits = field(default_factory=Limits)
    telemetry_interval_seconds: int | None = None
    heartbeat_timeout_seconds: int | None = None
    number_of_boilers: int | None = None
    boilers: tuple[dict[str, Any], ...] = ()
    sensors: tuple[dict[str, Any], ...] = ()


def _optional_float(raw: Any, field_name: str) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name}: expected a number, got {raw!r}") from exc


def _optional_int(raw: Any, field_name: str) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name}: expected an integer, got {raw!r}") from exc


def _object_list(raw: Any, field_name: str) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{field_name}: expected a list")
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError(f"{field_name}: entries must be objects")
    return tuple(raw)


def parse_config(payload: dict[str, Any]) -> DeviceConfig:
    """Validate and normalize a ``config.apply`` payload."""
    if not isinstance(payload, dict):
        raise ConfigError("config payload must be an object")

    version = payload.get("config_version")
    if not isinstance(version, int):
        raise ConfigError(f"'config_version' must be an integer, got {version!r}")

    raw_limits = payload.get("limits") or {}
    if not isinstance(raw_limits, dict):
        raise ConfigError("'limits' must be an object")

    limits = Limits(
        max_water_temperature_c=_optional_float(
            raw_limits.get("max_water_temperature_c"), "limits.max_water_temperature_c"
        ),
        min_water_temperature_c=_optional_float(
            raw_limits.get("min_water_temperature_c"), "limits.min_water_temperature_c"
        ),
        max_ambient_temperature_c=_optional_float(
            raw_limits.get("max_ambient_temperature_c"),
            "limits.max_ambient_temperature_c",
        ),
    )

    return DeviceConfig(
        version=version,
        site_label=str(payload.get("site_label") or ""),
        limits=limits,
        telemetry_interval_seconds=_optional_int(
            payload.get("telemetry_interval_seconds"), "telemetry_interval_seconds"
        ),
        heartbeat_timeout_seconds=_optional_int(
            payload.get("heartbeat_timeout_seconds"), "heartbeat_timeout_seconds"
        ),
        number_of_boilers=_optional_int(
            payload.get("number_of_boilers"), "number_of_boilers"
        ),
        boilers=_object_list(payload.get("boilers"), "boilers"),
        sensors=_object_list(payload.get("sensors"), "sensors"),
    )


# ---------------------------------------------------------------------------
# On-disk cache
# ---------------------------------------------------------------------------
#
# The raw payload is stored rather than the parsed dataclass, so reloading goes
# through exactly the same validation the server push does.


async def load_cached_config(
    path: Path | None = None,
) -> tuple[DeviceConfig | None, str | None]:
    """
    Load the last applied config from disk.

    Returns ``(config, error)``. A missing or unreadable cache is not an error
    condition for the caller — it simply yields ``(None, reason)``.
    """
    payload = await read_json(path or CONFIG_CACHE_PATH)
    if payload is None:
        return None, None

    try:
        return parse_config(payload), None
    except ConfigError as exc:
        return None, str(exc)


async def save_cached_config(
    payload: dict[str, Any],
    path: Path | None = None,
) -> None:
    """Persist a config payload that was successfully applied."""
    await write_json(path or CONFIG_CACHE_PATH, payload)


def describe(config: DeviceConfig | None) -> list[str]:
    """Human-readable summary for the control menu."""
    if config is None:
        return ["No config received yet."]

    lines = [f"Config v{config.version}" + (f" — {config.site_label}" if config.site_label else "")]
    lines.append(
        "  Limits: "
        f"water {config.limits.min_water_temperature_c}–"
        f"{config.limits.max_water_temperature_c} °C, "
        f"ambient max {config.limits.max_ambient_temperature_c} °C "
        "(stored, not enforced)"
    )
    lines.append(f"  Telemetry interval: {config.telemetry_interval_seconds}s")
    lines.append(f"  Heartbeat timeout:  {config.heartbeat_timeout_seconds}s")
    lines.append(f"  Boilers: {config.number_of_boilers}, sensors: {len(config.sensors)}")
    return lines
