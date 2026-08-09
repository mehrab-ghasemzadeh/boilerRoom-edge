"""
Atomic JSON file helpers, shared by the config and schedule caches.

Writes go to a sibling temp file and are renamed into place, so a power cut on
the Pi cannot leave a half-written document behind. Reads treat a missing or
corrupt file as "nothing cached" rather than an error — a damaged cache must
never stop the agent from starting.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any


def read_json_sync(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json_sync(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_name(path.name + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    os.replace(temp_path, path)


async def read_json(path: Path) -> dict[str, Any] | None:
    return await asyncio.to_thread(read_json_sync, path)


async def write_json(path: Path, payload: dict[str, Any]) -> None:
    await asyncio.to_thread(write_json_sync, path, payload)
