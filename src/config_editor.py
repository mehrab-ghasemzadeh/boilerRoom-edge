"""
Local editing of the operating limits.

``config.apply`` carries the temperature limits, but an operator standing in
the boiler room has to be able to change them without the cloud — the same
argument that put control modes and the schedule on the device. A room being
commissioned before anything is published, or one whose uplink is dead, still
needs its cut-outs settable.

An edit is a mutation of the raw ``config.apply`` document, handed back to
``parse_config`` before it is accepted, so a locally built config passes exactly
the checks a server push does.

Precedence is one-way and matches the schedule editor:

  * a local edit holds until the server publishes a config
  * a ``config.apply`` supersedes local edits and deletes the override

There is no device -> server config API in DEVICE.md, so the cloud cannot be
told about an edit and the device keeps reporting the *published*
``config_version``. Everything that can show the divergence does: the menu, the
log at WARNING, and ``config_locally_modified`` on the ``device.state`` push.

These are safety limits, so the numbers are bounded and cross-checked here
rather than trusted: a typo that turns 80 into 800 would leave a boiler with no
effective cut at all.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from json_store import read_json, write_json
from load_env import env_path

# The override lives beside the server's cache rather than overwriting it, so
# discarding an edit can put the published limits back.
CONFIG_LOCAL_PATH = env_path("BOILERROOM_CONFIG_LOCAL", "config_local.json")

# Nothing outside this is a plausible boiler-room temperature, and a value
# outside it is far more likely to be a typo than an intention. The cut-out is
# the last thing that should silently accept 800 °C.
MIN_SETTABLE_C = 0.0
MAX_SETTABLE_C = 200.0

# The three limits an operator can set, in the order the menu offers them.
LIMIT_FIELDS = (
    ("max_water_temperature_c", "Max water temperature"),
    ("min_water_temperature_c", "Min water temperature"),
    ("max_ambient_temperature_c", "Max ambient temperature"),
)


class ConfigEditError(ValueError):
    """Raised when an operator's input cannot be turned into a limit."""


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def parse_temperature_text(raw: str, field: str) -> float | None:
    """
    Read a temperature, or ``None`` to clear the limit.

    An empty answer is *cancel* at the call site; clearing is deliberate and
    has to be typed, so a limit is never dropped by hitting Enter.
    """
    text = raw.strip().lower()
    if text in ("none", "off", "clear", "-"):
        return None

    try:
        value = float(text)
    except ValueError as exc:
        raise ConfigEditError(
            f"{field}: cannot read {raw.strip()!r} as a temperature — "
            "type a number, or 'none' to remove the limit"
        ) from exc

    if not MIN_SETTABLE_C <= value <= MAX_SETTABLE_C:
        raise ConfigEditError(
            f"{field}: {value:g} °C is outside {MIN_SETTABLE_C:g}–{MAX_SETTABLE_C:g} °C. "
            "If that is genuinely intended, set it from the server instead"
        )
    return value


def blank_document(*, version: int = 0) -> dict[str, Any]:
    """
    An empty config, for a device that has never been given one.

    Version 0 means the server's first publish is newer and supersedes it.
    """
    return {"config_version": version, "limits": {}}


def set_limit(document: dict[str, Any], field: str, value: float | None) -> dict[str, Any]:
    """
    Return a copy of ``document`` with one limit changed.

    Cross-checked against the other limits, because they are only meaningful
    together: a minimum at or above the maximum would mean a boiler that is cut
    and can never recover.
    """
    known = {name for name, _ in LIMIT_FIELDS}
    if field not in known:
        raise ConfigEditError(f"unknown limit {field!r}")

    edited = copy.deepcopy(document)
    limits = edited.get("limits")
    if not isinstance(limits, dict):
        limits = {}
    limits = dict(limits)

    if value is None:
        limits.pop(field, None)
    else:
        limits[field] = value

    _check_coherent(limits)

    edited["limits"] = limits
    return edited


def _check_coherent(limits: dict[str, Any]) -> None:
    maximum = limits.get("max_water_temperature_c")
    minimum = limits.get("min_water_temperature_c")
    if maximum is None or minimum is None:
        return
    if minimum >= maximum:
        raise ConfigEditError(
            f"min water temperature ({minimum:g} °C) must be below the maximum "
            f"({maximum:g} °C) — otherwise a cut boiler could never recover"
        )


def describe_limits(document: dict[str, Any] | None) -> list[str]:
    """The three limits as they currently stand, for the menu."""
    limits = (document or {}).get("limits") or {}
    lines = []
    for field, label in LIMIT_FIELDS:
        value = limits.get(field)
        shown = f"{float(value):.1f} °C" if isinstance(value, (int, float)) else "not set"
        lines.append(f"  {label:<24} {shown}")
    return lines


# ---------------------------------------------------------------------------
# On-disk override
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalConfig:
    """Locally edited limits, as they are stored."""

    document: dict[str, Any]
    # The published version these edits were made on top of. If the cached
    # server config has moved past it, the edits are stale and get dropped.
    based_on_version: int
    revision: int
    saved_at: str = ""


async def load_local_config(
    path: Path | None = None,
) -> tuple[LocalConfig | None, str | None]:
    """
    Load the local override.

    Returns ``(local, error)``. No file yields ``(None, None)`` — an unedited
    device is the normal case. Only the wrapper is checked here; the document
    is validated by ``parse_config`` when it is adopted.
    """
    payload = await read_json(path or CONFIG_LOCAL_PATH)
    if payload is None:
        return None, None

    document = payload.get("config")
    if not isinstance(document, dict):
        return None, "no 'config' object"

    based_on = payload.get("based_on_version")
    revision = payload.get("revision")
    if not isinstance(based_on, int) or not isinstance(revision, int):
        return None, "'based_on_version' and 'revision' must be integers"

    return (
        LocalConfig(
            document=document,
            based_on_version=based_on,
            revision=revision,
            saved_at=str(payload.get("saved_at") or ""),
        ),
        None,
    )


async def save_local_config(
    document: dict[str, Any],
    *,
    based_on_version: int,
    revision: int,
    path: Path | None = None,
) -> None:
    """Persist a local override. Written atomically, like the other caches."""
    await write_json(
        path or CONFIG_LOCAL_PATH,
        {
            "saved_at": _utc_now_iso(),
            "based_on_version": based_on_version,
            "revision": revision,
            "config": document,
        },
    )


async def clear_local_config(path: Path | None = None) -> None:
    """Remove the override, leaving the published config in charge."""
    target = path or CONFIG_LOCAL_PATH
    await asyncio.to_thread(target.unlink, missing_ok=True)
