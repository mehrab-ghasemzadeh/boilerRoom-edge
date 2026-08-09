"""
Interactive control menu — runs concurrently with the sensor loop.

Blocking terminal input runs in a worker thread via asyncio.to_thread so the
event loop keeps polling sensors and posting telemetry.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from auth import API_BASE_URL, DEVICE_USERNAME, WS_BASE_URL, token_manager
from config import GAS_SENSORS, RELAYS, TEMPERATURE_SENSORS, UNITS, load_device_mapping
from data_logger import reading_store
from device_config import describe as describe_config
from mapping_provider import DEFAULT_MAPPING_PATH
from errors_client import build_error, schedule_post_errors
from limits_guard import limit_guard
from logging_setup import LOG_PATH, tail_log
from runtime_state import RuntimeState
from schedule_runner import relay_for_target, schedule_runner
from telemetry_client import post_telemetry

DEFAULT_LOG_LINES = 40
MAX_LOG_LINES = 500

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
  9) Show active schedule
 10) Show recent log lines
  0) Quit
> """


async def _prompt(text: str) -> str:
    import asyncio

    return (await asyncio.to_thread(input, text)).strip()


async def _show_last_readings(state: RuntimeState) -> None:
    snap = await state.get_snapshot()
    read_at = snap["read_at"]
    if read_at is None:
        await state.echo("\n[menu] No readings yet.\n")
        return

    await state.echo(f"\n[menu] Last readings at {read_at.isoformat()} (cycle {snap['cycle_count']})")
    for sensor_id, value in sorted(snap["temperatures"].items()):
        cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
        label = cfg.get("name", f"Sensor {sensor_id}")
        if value is None:
            await state.echo(f"  [{sensor_id}] {label}: unavailable")
        else:
            await state.echo(f"  [{sensor_id}] {label}: {value:.2f} °C")

    for sensor_id, value in sorted(snap["gas"].items()):
        cfg = GAS_SENSORS.get(sensor_id, {})
        label = cfg.get("name", f"Sensor {sensor_id}")
        await state.echo(f"  [{sensor_id}] {label}: {value}")
    await state.echo("")


async def _show_mapping(state: RuntimeState) -> None:
    await state.echo("\n[menu] Equipment units:")
    for unit_id, unit in UNITS.items():
        await state.echo(f"  {unit_id}: {unit['name']}")

    await state.echo("\n[menu] Temperature sensors:")
    for sid, cfg in sorted(TEMPERATURE_SENSORS.items()):
        unit = cfg.get("unit") or "—"
        await state.echo(
            f"  Sensor {sid}: {cfg['name']}  (role={cfg['role']}, unit={unit})"
        )

    await state.echo("\n[menu] Relays:")
    for rid, cfg in sorted(RELAYS.items()):
        unit = cfg.get("unit") or "—"
        await state.echo(
            f"  Relay {rid}: {cfg['name']}  "
            f"(role={cfg['role']}, unit={unit}, GPIO {cfg['gpio']})"
        )
    await state.echo("")


async def _reload_mapping(state: RuntimeState) -> None:
    mapping_path = os.environ.get("BOILERROOM_MAPPING", str(DEFAULT_MAPPING_PATH))
    await state.echo(f"\n[menu] Reloading mapping from {mapping_path} ...")
    try:
        await load_device_mapping()
        await state.echo("[menu] Mapping reloaded successfully.\n")
        await state.log(f"[menu] Mapping reloaded from {mapping_path}")
    except Exception as exc:
        await state.echo(f"[menu] Failed to reload mapping: {exc}\n")
        await state.log(f"[menu] Mapping reload failed: {exc}", level=logging.ERROR)


async def _change_read_interval(state: RuntimeState) -> None:
    current = await state.get_read_interval()
    await state.echo(f"\n[menu] Current read interval: {current:.0f}s")
    raw = await _prompt("New interval in seconds (empty = cancel): ")
    if not raw:
        await state.echo("[menu] Cancelled.\n")
        return
    try:
        seconds = float(raw)
        await state.set_read_interval(seconds)
        await state.echo(f"[menu] Read interval set to {seconds:.0f}s.\n")
        await state.log(f"[menu] Read interval changed to {seconds:.0f}s")
    except ValueError:
        await state.echo("[menu] Invalid number.\n")


async def _relay_menu(state: RuntimeState) -> None:
    rc = state.relay_controller
    if rc is None:
        await state.echo("\n[menu] Relay controller not available.\n")
        return

    blocked_relays = {
        relay_for_target(target): reason
        for target, reason in (await state.get_limit_blocks()).items()
        if relay_for_target(target) is not None
    }

    await state.echo("\n[menu] Relay states:")
    for rid, cfg in sorted(RELAYS.items()):
        on = rc.get_state(rid)
        note = f"  [CUT: {blocked_relays[rid]}]" if rid in blocked_relays else ""
        await state.echo(
            f"  Relay {rid}: {cfg['name']} — {'ON' if on else 'OFF'}{note}"
        )

    raw = await _prompt(
        "Enter relay ID to toggle (empty = back): "
    )
    if not raw:
        await state.echo("")
        return
    try:
        relay_id = int(raw)
    except ValueError:
        await state.echo("[menu] Invalid relay ID.\n")
        return

    if relay_id in blocked_relays and not rc.get_state(relay_id):
        await state.echo(
            f"[menu] Relay {relay_id} is cut off by a temperature limit "
            f"({blocked_relays[relay_id]}) — refusing to switch it on.\n"
        )
        await state.log(
            f"[menu] Refused to switch relay {relay_id} on: "
            f"{blocked_relays[relay_id]}",
            level=logging.WARNING,
        )
        return

    await rc.toggle(relay_id)
    on = rc.get_state(relay_id)
    await state.echo(f"[menu] Relay {relay_id} is now {'ON' if on else 'OFF'}.\n")
    await state.log(f"[menu] Relay {relay_id} switched {'on' if on else 'off'} by operator")


async def _post_telemetry_now(state: RuntimeState) -> None:
    snap = await state.get_snapshot()
    if snap["read_at"] is None or state.relay_controller is None:
        await state.echo("\n[menu] No readings to send yet.\n")
        return

    await state.echo("\n[menu] Posting telemetry ...")
    try:
        await post_telemetry(state, snap["temperatures"], snap["gas"])
        await state.echo("[menu] Telemetry posted.\n")
    except Exception as exc:
        await state.echo(f"[menu] Telemetry failed: {exc}\n")


async def _show_app_config(state: RuntimeState) -> None:
    interval = await state.get_read_interval()
    mapping_path = Path(os.environ.get("BOILERROOM_MAPPING", DEFAULT_MAPPING_PATH))
    await state.echo("\n[menu] App configuration:")
    await state.echo(f"  API base URL:     {API_BASE_URL}")
    await state.echo(f"  WebSocket URL:    {WS_BASE_URL}")
    session = token_manager.session
    await state.echo(f"  Device username:  {DEVICE_USERNAME or '(not set)'}")
    await state.echo(f"  Device ID:        {session.device_id if session else '(not logged in)'}")
    await state.echo(f"  Read interval:    {interval:.0f}s")
    await state.echo(f"  Mapping source:   {os.environ.get('BOILERROOM_MAPPING_SOURCE', 'file')}")
    await state.echo(f"  Mapping file:     {mapping_path}")
    await state.echo(f"  Authenticated:    {token_manager.is_authenticated}")
    ws = await state.get_ws_status()
    await state.echo(f"  WebSocket:        {'connected' if ws['connected'] else 'disconnected'}")
    if ws["hello_ack"]:
        await state.echo(f"  WS hello_ack:     yes (server_time={ws.get('server_time')})")
        await state.echo(
            f"  Desired config:   v{ws.get('desired_config_version')}  "
            f"schedule: v{ws.get('desired_schedule_version')}"
        )
        config_v, schedule_v = await state.get_active_versions()
        await state.echo(f"  Active config:    v{config_v}  schedule: v{schedule_v}")

    await state.echo("")
    for line in describe_config(await state.get_device_config()):
        await state.echo(f"  {line}")
    for line in limit_guard.describe():
        await state.echo(f"  {line}")

    modes = await state.get_modes()
    if modes:
        await state.echo("  Modes: " + ", ".join(f"{t}={m}" for t, m in sorted(modes.items(), key=str)))

    try:
        stats = await reading_store.stats()
        await state.echo(
            f"  Database: {stats['rows']} rows from {stats['sensors']} sensor(s), "
            f"{stats['size_bytes'] / 1024:.0f} KB, keeping {stats['retention_days']} days"
        )
        if stats["oldest"]:
            await state.echo(f"            {stats['oldest']} .. {stats['newest']}")
        await state.echo(f"            {stats['path']}")
    except Exception as exc:
        await state.echo(f"  Database: unavailable ({exc})")

    await state.echo("")


async def _report_test_error(state: RuntimeState) -> None:
    await state.echo("\n[menu] Scheduling test error report ...")
    schedule_post_errors(
        build_error(
            code="manual_test",
            message="Manual test error from control menu",
            severity="info",
            device_state="ok",
        ),
        log=state.log,
    )
    await state.echo("[menu] Error post scheduled (runs in background).\n")


async def _show_schedule(state: RuntimeState) -> None:
    await state.echo("")
    for line in schedule_runner.describe():
        await state.echo(f"[menu] {line}")
    await state.echo("")


async def _show_logs(state: RuntimeState) -> None:
    raw = await _prompt(f"\nHow many lines? (default {DEFAULT_LOG_LINES}, max {MAX_LOG_LINES}): ")

    limit = DEFAULT_LOG_LINES
    if raw:
        try:
            limit = max(1, min(MAX_LOG_LINES, int(raw)))
        except ValueError:
            await state.echo("[menu] Invalid number.\n")
            return

    lines = await tail_log(limit)

    # Printed rather than logged: routing these through the logger would append
    # what you are reading back into the log file.
    if not lines:
        await state.echo(f"\n[menu] No log lines yet ({LOG_PATH}).\n")
        return

    await state.echo(f"\n----- last {len(lines)} line(s) of {LOG_PATH} -----")
    await state.echo("\n".join(lines))
    await state.echo("-" * 60 + "\n")


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
    elif choice == "9":
        await _show_schedule(state)
    elif choice == "10":
        await _show_logs(state)
    elif choice == "0":
        await state.echo("\n[menu] Shutting down ...")
        state.shutdown.set()
    else:
        await state.echo(f"\n[menu] Unknown option: {choice!r}\n")


def menu_enabled() -> bool:
    """
    Whether to offer the interactive menu.

    Under systemd there is no terminal: ``input()`` would raise EOFError
    immediately, the menu would treat that as "operator chose quit", and the
    service would exit and be restarted forever. Default to the menu only when
    stdin is a TTY; BOILERROOM_MENU=on/off overrides.
    """
    override = os.environ.get("BOILERROOM_MENU", "").strip().lower()
    if override in ("on", "1", "true", "yes"):
        return True
    if override in ("off", "0", "false", "no"):
        return False
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


async def run_control_menu(state: RuntimeState) -> None:
    if not menu_enabled():
        await state.log(
            "[menu] No terminal attached — control menu disabled "
            "(set BOILERROOM_MENU=on to force it)"
        )
        await state.shutdown.wait()
        return

    await _run_menu_loop(state)


async def _run_menu_loop(state: RuntimeState) -> None:
    await state.echo(
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
