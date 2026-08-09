"""
Interactive control menu — runs concurrently with the sensor loop.

Blocking terminal input runs in a worker thread via asyncio.to_thread so the
event loop keeps polling sensors and posting telemetry.
"""

from __future__ import annotations

import os
from pathlib import Path

from auth import API_BASE_URL, DEVICE_ID, WS_BASE_URL, token_manager
from config import GAS_SENSORS, RELAYS, TEMPERATURE_SENSORS, UNITS, load_device_mapping
from mapping_provider import DEFAULT_MAPPING_PATH
from errors_client import build_error, schedule_post_errors
from runtime_state import RuntimeState
from telemetry_client import post_telemetry

MENU = """
--- Control Menu ---
  1) Last sensor readings
  2) Show device mapping
  3) Reload mapping from file
  4) Change read interval
  5) Relay status / control
  6) Post telemetry now
  7) Show app configuration
  8) Report test error to server
  0) Quit
> """


async def _prompt(text: str) -> str:
    import asyncio

    return (await asyncio.to_thread(input, text)).strip()


async def _show_last_readings(state: RuntimeState) -> None:
    snap = await state.get_snapshot()
    read_at = snap["read_at"]
    if read_at is None:
        await state.log("\n[menu] No readings yet.\n")
        return

    await state.log(f"\n[menu] Last readings at {read_at.isoformat()} (cycle {snap['cycle_count']})")
    for sensor_id, value in sorted(snap["temperatures"].items()):
        cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
        label = cfg.get("name", f"Sensor {sensor_id}")
        if value is None:
            await state.log(f"  [{sensor_id}] {label}: unavailable")
        else:
            await state.log(f"  [{sensor_id}] {label}: {value:.2f} °C")

    for sensor_id, value in sorted(snap["gas"].items()):
        cfg = GAS_SENSORS.get(sensor_id, {})
        label = cfg.get("name", f"Sensor {sensor_id}")
        await state.log(f"  [{sensor_id}] {label}: {value}")
    await state.log("")


async def _show_mapping(state: RuntimeState) -> None:
    await state.log("\n[menu] Equipment units:")
    for unit_id, unit in UNITS.items():
        await state.log(f"  {unit_id}: {unit['name']}")

    await state.log("\n[menu] Temperature sensors:")
    for sid, cfg in sorted(TEMPERATURE_SENSORS.items()):
        unit = cfg.get("unit") or "—"
        await state.log(
            f"  Sensor {sid}: {cfg['name']}  (role={cfg['role']}, unit={unit})"
        )

    await state.log("\n[menu] Relays:")
    for rid, cfg in sorted(RELAYS.items()):
        unit = cfg.get("unit") or "—"
        await state.log(
            f"  Relay {rid}: {cfg['name']}  "
            f"(role={cfg['role']}, unit={unit}, GPIO {cfg['gpio']})"
        )
    await state.log("")


async def _reload_mapping(state: RuntimeState) -> None:
    mapping_path = os.environ.get("BOILERROOM_MAPPING", str(DEFAULT_MAPPING_PATH))
    await state.log(f"\n[menu] Reloading mapping from {mapping_path} ...")
    try:
        await load_device_mapping()
        await state.log("[menu] Mapping reloaded successfully.\n")
    except Exception as exc:
        await state.log(f"[menu] Failed to reload mapping: {exc}\n")


async def _change_read_interval(state: RuntimeState) -> None:
    current = await state.get_read_interval()
    await state.log(f"\n[menu] Current read interval: {current:.0f}s")
    raw = await _prompt("New interval in seconds (empty = cancel): ")
    if not raw:
        await state.log("[menu] Cancelled.\n")
        return
    try:
        seconds = float(raw)
        await state.set_read_interval(seconds)
        await state.log(f"[menu] Read interval set to {seconds:.0f}s.\n")
    except ValueError:
        await state.log("[menu] Invalid number.\n")


async def _relay_menu(state: RuntimeState) -> None:
    rc = state.relay_controller
    if rc is None:
        await state.log("\n[menu] Relay controller not available.\n")
        return

    await state.log("\n[menu] Relay states:")
    for rid, cfg in sorted(RELAYS.items()):
        on = rc.get_state(rid)
        await state.log(
            f"  Relay {rid}: {cfg['name']} — {'ON' if on else 'OFF'}"
        )

    raw = await _prompt(
        "Enter relay ID to toggle (empty = back): "
    )
    if not raw:
        await state.log("")
        return
    try:
        relay_id = int(raw)
        await rc.toggle(relay_id)
        on = rc.get_state(relay_id)
        await state.log(f"[menu] Relay {relay_id} is now {'ON' if on else 'OFF'}.\n")
    except ValueError:
        await state.log("[menu] Invalid relay ID.\n")


async def _post_telemetry_now(state: RuntimeState) -> None:
    snap = await state.get_snapshot()
    if snap["read_at"] is None or state.relay_controller is None:
        await state.log("\n[menu] No readings to send yet.\n")
        return

    await state.log("\n[menu] Posting telemetry ...")
    try:
        await post_telemetry(
            snap["temperatures"],
            snap["gas"],
            state.relay_controller,
        )
        await state.log("[menu] Telemetry posted.\n")
    except Exception as exc:
        await state.log(f"[menu] Telemetry failed: {exc}\n")


async def _show_app_config(state: RuntimeState) -> None:
    interval = await state.get_read_interval()
    mapping_path = Path(os.environ.get("BOILERROOM_MAPPING", DEFAULT_MAPPING_PATH))
    await state.log("\n[menu] App configuration:")
    await state.log(f"  API base URL:     {API_BASE_URL}")
    await state.log(f"  WebSocket URL:    {WS_BASE_URL}")
    await state.log(f"  Device ID:        {DEVICE_ID or '(not set)'}")
    await state.log(f"  Read interval:    {interval:.0f}s")
    await state.log(f"  Mapping source:   {os.environ.get('BOILERROOM_MAPPING_SOURCE', 'file')}")
    await state.log(f"  Mapping file:     {mapping_path}")
    await state.log(f"  Authenticated:    {token_manager.is_authenticated}")
    ws = await state.get_ws_status()
    await state.log(f"  WebSocket:        {'connected' if ws['connected'] else 'disconnected'}")
    if ws["hello_ack"]:
        await state.log(f"  WS hello_ack:     yes (server_time={ws.get('server_time')})")
        await state.log(
            f"  Desired config:   v{ws.get('desired_config_version')}  "
            f"schedule: v{ws.get('desired_schedule_version')}"
        )
    await state.log("")


async def _report_test_error(state: RuntimeState) -> None:
    await state.log("\n[menu] Scheduling test error report ...")
    schedule_post_errors(
        build_error(
            code="manual_test",
            message="Manual test error from control menu",
            severity="info",
            device_state="ok",
        ),
        log=state.log,
    )
    await state.log("[menu] Error post scheduled (runs in background).\n")


async def _handle_choice(state: RuntimeState, choice: str) -> None:
    if choice == "1":
        await _show_last_readings(state)
    elif choice == "2":
        await _show_mapping(state)
    elif choice == "3":
        await _reload_mapping(state)
    elif choice == "4":
        await _change_read_interval(state)
    elif choice == "5":
        await _relay_menu(state)
    elif choice == "6":
        await _post_telemetry_now(state)
    elif choice == "7":
        await _show_app_config(state)
    elif choice == "8":
        await _report_test_error(state)
    elif choice == "0":
        await state.log("\n[menu] Shutting down ...")
        state.shutdown.set()
    else:
        await state.log(f"\n[menu] Unknown option: {choice!r}\n")


async def run_control_menu(state: RuntimeState) -> None:
    await state.log(
        "Control menu ready — type a number and press Enter "
        "(sensor polling continues in background).\n"
    )

    while not state.shutdown.is_set():
        try:
            choice = await _prompt(MENU)
        except EOFError:
            state.shutdown.set()
            break

        if state.shutdown.is_set():
            break

        await _handle_choice(state, choice)
