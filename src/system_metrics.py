"""
Host metrics reported in device.heartbeat (see DEVICE.md).

Everything here reads /proc, so each reader is a small blocking function run
through asyncio.to_thread rather than called from the event loop. On hosts
without these files (Windows dev machines) the readers return nothing and the
heartbeat simply omits those fields.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

MEMINFO_PATH = Path("/proc/meminfo")
WIRELESS_PATH = Path("/proc/net/wireless")

_START_TIME = time.monotonic()


def uptime_seconds() -> int:
    """Seconds since this process started."""
    return int(time.monotonic() - _START_TIME)


def _read_free_memory_bytes() -> int | None:
    try:
        for line in MEMINFO_PATH.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, IndexError, ValueError):
        return None
    return None


def _read_wireless() -> tuple[str | None, int | None]:
    """Parse /proc/net/wireless -> ("wifi", rssi_dbm) for the first live iface."""
    try:
        lines = WIRELESS_PATH.read_text().splitlines()
    except OSError:
        return None, None

    # First two lines are headers; data rows look like:
    #   wlan0: 0000   70.  -40.  -256  0  0  0  0  43  0
    for line in lines[2:]:
        if ":" not in line:
            continue
        _iface, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) < 3:
            continue
        try:
            return "wifi", int(float(fields[2]))
        except ValueError:
            continue

    return None, None


def _collect() -> dict[str, Any]:
    metrics: dict[str, Any] = {"uptime_seconds": uptime_seconds()}

    free_memory = _read_free_memory_bytes()
    if free_memory is not None:
        metrics["free_memory_bytes"] = free_memory

    network_type, rssi_dbm = _read_wireless()
    if network_type is not None:
        metrics["network_type"] = network_type
    if rssi_dbm is not None:
        metrics["rssi_dbm"] = rssi_dbm

    return metrics


async def read_system_metrics() -> dict[str, Any]:
    """Uptime plus free memory and wifi signal when the host exposes them."""
    return await asyncio.to_thread(_collect)
