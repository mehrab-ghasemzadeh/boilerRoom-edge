"""
On-disk cache of per-unit control modes.

Each boiler and pump is either ``automatic`` (the schedule drives it) or
``manual`` (only commands do). Those modes used to live in memory alone, so a
reboot returned every unit to schedule control: a boiler switched off by hand
for maintenance would fire up again after a power blip, with nothing in the
logs to explain it.

The cache is written whenever a mode actually changes — a rare, operator-driven
event, so this costs the SD card almost nothing — and restored before the first
schedule evaluation, so a manual unit is left alone from the very first tick.

Modes are per unit by design. There is no device-wide switch: boiler 1 can be
under maintenance while boiler 2 follows the programme.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from json_store import read_json, write_json
from load_env import env_path
from logging_setup import get_logger

_log = get_logger("mode")

MODE_CACHE_PATH = env_path("BOILERROOM_MODE_CACHE", "mode_cache.json")

VALID_MODES = ("automatic", "manual")
VALID_TARGET_TYPES = ("boiler", "pump")


def _key(target: Any) -> str:
    return f"{target.type}:{target.index}"


def _parse_key(raw: str) -> tuple[str, int] | None:
    kind, _, index = raw.partition(":")
    if kind not in VALID_TARGET_TYPES:
        return None
    try:
        return kind, int(index)
    except ValueError:
        return None


async def load_modes(path: Path | None = None) -> dict[Any, str]:
    """
    Restore saved modes, keyed by ``Target``.

    A missing, corrupt or partly invalid cache yields whatever could be read:
    an unreadable mode file must never stop the agent from starting, and every
    unit it cannot describe simply falls back to ``automatic``.
    """
    from schedule_runner import Target

    payload = await read_json(path or MODE_CACHE_PATH)
    if not payload:
        return {}

    raw_modes = payload.get("modes")
    if not isinstance(raw_modes, dict):
        _log.warning("Mode cache has no 'modes' object — ignoring it")
        return {}

    modes: dict[Any, str] = {}
    for raw_key, mode in raw_modes.items():
        parsed = _parse_key(str(raw_key))
        if parsed is None:
            _log.warning("Ignoring unusable mode cache entry %r", raw_key)
            continue
        if mode not in VALID_MODES:
            _log.warning("Ignoring mode %r for %s — not a known mode", mode, raw_key)
            continue
        kind, index = parsed
        modes[Target(kind, index)] = mode

    return modes


async def save_modes(modes: dict[Any, str], path: Path | None = None) -> None:
    """Persist the current modes. Written atomically, like the other caches."""
    payload = {
        "saved_at": datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "modes": {_key(target): mode for target, mode in sorted(modes.items(), key=str)},
    }
    await write_json(path or MODE_CACHE_PATH, payload)
