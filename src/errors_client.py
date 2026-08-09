"""
Post device errors to the server (POST /api/v1/devices/<id>/errors).

All network I/O is async. Use ``schedule_post_errors`` to report without
blocking the caller (sensor loop, menu, etc.).
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any, Awaitable, Callable

from auth import DEVICE_ACCESS_TOKEN, DEVICE_ID, DEVICE_SERIAL, token_manager
from config import TEMPERATURE_SENSORS

LogFn = Callable[..., Awaitable[None]]


def _device_id() -> str:
    device_id = DEVICE_ID or DEVICE_SERIAL
    if not device_id:
        raise ValueError(
            "BOILERROOM_DEVICE_ID or BOILERROOM_DEVICE_SERIAL must be set"
        )
    return device_id


def build_error(
    code: str,
    message: str,
    *,
    severity: str = "warning",
    device_state: str = "degraded",
    sensor_id: str | None = None,
    boiler_index: int | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "severity": severity,
        "device_state": device_state,
        "reported_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
    if sensor_id is not None:
        error["sensor_id"] = sensor_id
    if boiler_index is not None:
        error["boiler_index"] = boiler_index
    return error


async def post_errors(errors: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    """POST one or more errors to the server."""
    if not DEVICE_ACCESS_TOKEN:
        raise ValueError(
            "BOILERROOM_DEVICE_ACCESS_TOKEN is not set. "
            "Provision the device and add the token to .env"
        )

    body: dict[str, Any] | list[dict[str, Any]]
    if isinstance(errors, list):
        body = {"errors": errors}
    else:
        body = errors

    path = f"/api/v1/devices/{_device_id()}/errors"
    return await token_manager.request_json_device("POST", path, body)


async def post_error(
    code: str,
    message: str,
    **kwargs: Any,
) -> dict[str, Any]:
    return await post_errors(build_error(code, message, **kwargs))


def schedule_post_errors(
    errors: list[dict[str, Any]] | dict[str, Any],
    *,
    log: LogFn | None = None,
) -> asyncio.Task[None]:
    """Fire-and-forget error post; does not block the caller."""

    async def _run() -> None:
        try:
            await post_errors(errors)
            if isinstance(errors, list):
                count = len(errors)
                preview = errors[0].get("code", "?") if errors else "?"
            else:
                count = 1
                preview = errors.get("code", "?")
            msg = f"[errors] Posted {count} error(s) (code={preview})"
            if log:
                await log(msg)
            else:
                print(msg)
        except Exception as exc:
            msg = f"[errors] Failed to post: {exc}"
            if log:
                await log(msg)
            else:
                print(msg)

    return asyncio.create_task(_run())


def _unit_to_boiler_index(unit: str | None) -> int | None:
    if not unit or not unit.startswith("pot_"):
        return None
    try:
        return int(unit.split("_", 1)[1])
    except (IndexError, ValueError):
        return None


class ErrorReporter:
    """Tracks active faults and posts new ones without duplicate spam."""

    def __init__(self) -> None:
        self._active_faults: set[str] = set()

    async def check_temperature_faults(
        self,
        temperatures: dict[int, float | None],
        *,
        log: LogFn | None = None,
    ) -> None:
        current_faults: set[str] = set()

        for sensor_id, value in temperatures.items():
            if value is not None:
                continue

            fault_key = f"temp-{sensor_id}"
            current_faults.add(fault_key)

            if fault_key in self._active_faults:
                continue

            cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
            label = cfg.get("name", f"Sensor {sensor_id}")
            unit = cfg.get("unit")
            boiler_index = _unit_to_boiler_index(unit)

            schedule_post_errors(
                build_error(
                    code="sensor_fault",
                    message=f"{label} unavailable or read timeout",
                    severity="critical",
                    device_state="fault",
                    sensor_id=f"temp-{sensor_id}",
                    boiler_index=boiler_index,
                ),
                log=log,
            )

        self._active_faults = current_faults


error_reporter = ErrorReporter()
