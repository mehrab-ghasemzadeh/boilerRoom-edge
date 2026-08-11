"""
config.apply document (see DEVICE.md).

The server pushes an operating configuration: safety limits, telemetry cadence,
and the boiler/sensor inventory it believes this device has. This module parses
and holds it; the device replies config.result with applied/failed.

Parsing only: ``limits_guard`` is what enforces the limits, and how it derives
a cut-out from ``max_water_temperature_c`` depends on how many probes the unit
has — see that module.
"""

from __future__ import annotations

import copy
import logging
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
) -> tuple[DeviceConfig | None, dict[str, Any] | None, str | None]:
    """
    Load the last applied config from disk.

    Returns ``(config, document, error)``. A missing or unreadable cache is not
    an error condition for the caller — it simply yields ``(None, None, reason)``.

    The raw document comes back alongside the parsed one because it is what a
    local limit edit is applied to, and what discarding one reverts to.
    """
    payload = await read_json(path or CONFIG_CACHE_PATH)
    if payload is None:
        return None, None, None

    try:
        return parse_config(payload), payload, None
    except ConfigError as exc:
        return None, None, str(exc)


async def save_cached_config(
    payload: dict[str, Any],
    path: Path | None = None,
) -> None:
    """Persist a config payload that was successfully applied."""
    await write_json(path or CONFIG_CACHE_PATH, payload)


class ConfigStore:
    """
    Owns the config document driving the device.

    Arranged like ``ScheduleRunner``: the last document the server published is
    kept alongside any local override, so an operator's limit change can be
    discarded and the published one put back, and so a publish can supersede it.

    ``RuntimeState.device_config`` still holds the *effective* parsed config —
    that is what the limit guard reads — and is updated in step with this.
    """

    def __init__(self) -> None:
        self._server_document: dict[str, Any] | None = None
        self._document: dict[str, Any] | None = None
        self._local_revision = 0
        self._local_persisted = True

    @property
    def document(self) -> dict[str, Any] | None:
        """A copy of the effective document, safe for an editor to mutate."""
        return copy.deepcopy(self._document) if self._document is not None else None

    @property
    def server_version(self) -> int:
        version = (self._server_document or {}).get("config_version")
        return version if isinstance(version, int) else 0

    @property
    def is_locally_modified(self) -> bool:
        return self._local_revision > 0

    @property
    def local_revision(self) -> int:
        return self._local_revision

    @property
    def local_persisted(self) -> bool:
        return self._local_persisted

    def set_server_document(self, document: dict[str, Any]) -> int:
        """
        Adopt a published document. Returns how many local edits it superseded.

        The caller reports the supersede and removes the override file: a
        publish is an explicit act by whoever owns the room, so it outranks
        limits typed on the device — but never silently.
        """
        superseded = self._local_revision
        self._server_document = copy.deepcopy(document)
        self._document = copy.deepcopy(document)
        self._local_revision = 0
        return superseded

    async def apply_local(self, document: dict[str, Any], state) -> DeviceConfig:
        """
        Adopt limits edited on the device.

        Validated by ``parse_config`` first, so a bad edit raises and the
        running limits are left exactly as they were. The version is not
        bumped: it names the published document these edits sit on top of.
        """
        from config_editor import save_local_config

        config = parse_config(document)

        revision = self._local_revision + 1
        self._document = copy.deepcopy(document)
        self._local_revision = revision

        await state.set_device_config(config)
        await state.set_active_config_version(config.version)

        try:
            await save_local_config(
                document,
                based_on_version=self.server_version,
                revision=revision,
            )
            self._local_persisted = True
        except Exception as exc:
            # The limits are already in force; a card that cannot be written
            # must not undo them. They simply will not survive a restart.
            self._local_persisted = False
            await state.log(
                f"[config] Local limits are in effect but could not be saved: {exc}",
                level=logging.WARNING,
            )

        await state.log(
            f"[config] Running locally edited limits (revision {revision}, on top of "
            f"published v{self.server_version}) — the next publish replaces them",
            level=logging.WARNING,
        )
        return config

    async def restore_local(self, local, state) -> DeviceConfig:
        """Re-adopt a saved override at boot, without re-saving or renumbering."""
        config = parse_config(local.document)
        self._document = copy.deepcopy(local.document)
        self._local_revision = local.revision
        await state.set_device_config(config)
        await state.set_active_config_version(config.version)
        return config

    async def revert_local(self, state) -> DeviceConfig | None:
        """
        Throw the local limits away and go back to the published ones.

        Returns the config now in force, or None if nothing has ever been
        published — in which case the device is left with no limits, and so no
        over-temperature cut, which the caller says out loud.
        """
        from config_editor import clear_local_config

        await clear_local_config()
        self._local_persisted = True
        self._local_revision = 0

        document = self._server_document
        if document is None:
            self._document = None
            await state.set_device_config(None)
            return None

        config = parse_config(document)
        self._document = copy.deepcopy(document)
        await state.set_device_config(config)
        await state.set_active_config_version(config.version)
        return config


config_store = ConfigStore()


def describe(config: DeviceConfig | None) -> list[str]:
    """Human-readable summary for the control menu."""
    if config is None:
        return ["No config received yet."]

    lines = [f"Config v{config.version}" + (f" — {config.site_label}" if config.site_label else "")]
    lines.append(
        "  Limits: "
        f"water {config.limits.min_water_temperature_c}–"
        f"{config.limits.max_water_temperature_c} °C, "
        f"ambient max {config.limits.max_ambient_temperature_c} °C"
    )
    lines.append(f"  Telemetry interval: {config.telemetry_interval_seconds}s")
    lines.append(f"  Heartbeat timeout:  {config.heartbeat_timeout_seconds}s")
    lines.append(f"  Boilers: {config.number_of_boilers}, sensors: {len(config.sensors)}")
    return lines
