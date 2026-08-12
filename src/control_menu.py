"""
Interactive control menu — runs concurrently with the sensor loop.

Blocking terminal input runs in a worker thread via asyncio.to_thread so the
event loop keeps polling sensors and posting telemetry.

Answers come from an input device rather than from ``input()`` directly: the
keyboard on the bench, the GPIO matrix keypad on the installed hardware, chosen
by USE_MOCK_HARDWARE the same way the relays and sensor readers are. Both hand
back a whole line, so there is one menu rather than a keyboard menu and a
keypad menu drifting apart.

Every prompt therefore has to be answerable from sixteen keys. The menus were
already numeric; what needed work was the free text around them, and the
parsers now take a digit form of each — days as 1-7, a time as HHMM, a date as
YYYYMMDD — alongside the words. See ``keypad_layout`` for the key table.
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
from pathlib import Path

from auth import API_BASE_URL, DEVICE_USERNAME, WS_BASE_URL, token_manager
from config import GAS_SENSORS, RELAYS, TEMPERATURE_SENSORS, UNITS, load_device_mapping
from config_editor import (
    LIMIT_FIELDS,
    ConfigEditError,
    blank_document as blank_config_document,
    describe_limits,
    parse_temperature_text,
    set_limit,
)
from data_logger import reading_store
from setpoint_store import (
    MAX_SETPOINT_C,
    MIN_SETPOINT_C,
    SetpointError,
    validate as validate_setpoint,
)
from device_config import ConfigError, config_store, describe as describe_config
from device_record import (
    describe as describe_record,
    device_record_store,
    fetch_device_record,
    save_cached_device_record,
)
from mapping_provider import DEFAULT_MAPPING_PATH, DEFAULT_SOURCE
from limits_guard import limit_guard
from runtime_state import RuntimeState
from schedule_editor import (
    ScheduleEditError,
    add_exception,
    add_weekly_rule,
    blank_document,
    parse_date_text,
    parse_days,
    parse_state_text,
    parse_time_text,
    remove_exception,
    remove_weekly_rule,
)
from schedule_runner import (
    TARGET_ROLE,
    ScheduleError,
    Target,
    relay_for_target,
    schedule_runner,
)

# relay role -> schedule target type, the reverse of schedule_runner's map
ROLE_TARGET = {role: kind for kind, role in TARGET_ROLE.items()}

MENU = """
--- Control Menu ---
  1) Last sensor readings
  2) Show device mapping
  3) Show app configuration
  4) Show active schedule
  5) Show server device record
  6) Relay status / control
  7) Set unit mode (automatic/manual)
  8) Reload device mapping
  9) Change schedule
 10) Change temperatures
  0) Quit
> """

TEMPERATURE_MENU = """
  1) Set a boiler's temperature
  2) Clear a boiler's temperature (use the device-wide limit)
  3) Device-wide safety limits
  0) Back
> """

LIMITS_MENU = """
  1) Max water temperature
  2) Min water temperature
  3) Max ambient temperature
  4) Discard local edits (back to the published config)
  0) Back
> """

SCHEDULE_MENU = """
  1) Add a weekly rule
  2) Remove a weekly rule
  3) Add a date exception
  4) Remove a date exception
  5) Discard local edits (back to the published schedule)
  0) Back
> """


# The device answers are read from. A module-level handle, like the schedule
# and config stores, so the twenty-odd call sites of _prompt keep their
# signature instead of threading it through every menu function.
_input_device = None


def set_input_device(device) -> None:
    global _input_device
    _input_device = device


def input_device():
    """The active input device, defaulting to the keyboard."""
    if _input_device is None:
        from mock_keypad import MockKeypad

        set_input_device(MockKeypad())
    return _input_device


async def _prompt(text: str) -> str:
    return (await input_device().read_line(text)).strip()


def _is_yes(answer: str) -> bool:
    """
    Whether a yes/no prompt was answered yes.

    ``1`` and ``0`` are here because the keypad has no letters — the same
    reason parse_state_text has taken them since it was written.
    """
    return answer.strip().lower() in ("y", "yes", "1", "on")


def _is_no(answer: str) -> bool:
    return answer.strip().lower() in ("n", "no", "0", "off")


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
    source = os.environ.get("BOILERROOM_MAPPING_SOURCE", DEFAULT_SOURCE).lower()
    origin = (
        os.environ.get("BOILERROOM_MAPPING", str(DEFAULT_MAPPING_PATH))
        if source == "file"
        else "the server device record"
    )
    await state.echo(f"\n[menu] Reloading mapping from {origin} ...")
    try:
        await load_device_mapping()
        await state.echo("[menu] Mapping reloaded successfully.")
        # The relay controller configured its GPIO pins from the previous
        # mapping, so a changed pin map only takes effect on restart.
        await state.echo(
            "[menu] Note: relay pins are configured at startup — restart the "
            "agent if the wiring changed.\n"
        )
        await state.log(f"[menu] Mapping reloaded from {origin}")
    except Exception as exc:
        await state.echo(f"[menu] Failed to reload mapping: {exc}\n")
        await state.log(f"[menu] Mapping reload failed: {exc}", level=logging.ERROR)


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
    await state.notify_state_change(
        f"relay {relay_id} {'on' if on else 'off'} (operator)"
    )


async def _show_app_config(state: RuntimeState) -> None:
    interval = await state.get_read_interval()
    telemetry_interval = await state.get_telemetry_interval()
    mapping_path = Path(os.environ.get("BOILERROOM_MAPPING", DEFAULT_MAPPING_PATH))
    await state.echo("\n[menu] App configuration:")
    await state.echo(f"  API base URL:     {API_BASE_URL}")
    await state.echo(f"  WebSocket URL:    {WS_BASE_URL}")
    session = token_manager.session
    await state.echo(f"  Device username:  {DEVICE_USERNAME or '(not set)'}")
    await state.echo(f"  Device ID:        {session.device_id if session else '(not logged in)'}")
    await state.echo(f"  Read interval:    {interval:.0f}s")
    await state.echo(f"  Telemetry every:  {telemetry_interval:.0f}s")
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
    device_config = await state.get_device_config()
    for line in describe_config(device_config):
        await state.echo(f"  {line}")
    # The guard needs the config too: thresholds depend on it and on how many
    # probes each unit has.
    for line in limit_guard.describe(device_config, await state.get_setpoints()):
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

        outbox = await reading_store.outbox_stats()
        if outbox["pending"] or outbox["stuck"]:
            await state.echo(
                f"  Outbox:   {outbox['pending']} awaiting retry"
                + (f", {outbox['stuck']} given up on" if outbox["stuck"] else "")
                + (f", oldest {outbox['oldest_pending']}" if outbox["oldest_pending"] else "")
            )
        else:
            await state.echo(
                f"  Outbox:   empty ({outbox['synced']} recovered post(s) on record)"
            )
    except Exception as exc:
        await state.echo(f"  Database: unavailable ({exc})")

    await state.echo("")


async def _show_schedule(state: RuntimeState) -> None:
    await state.echo("")
    for line in schedule_runner.describe():
        await state.echo(f"[menu] {line}")
    await state.echo("")


def _controllable_targets() -> list[Target]:
    """Every boiler and pump that has a relay in the device mapping."""
    targets: set[Target] = set()
    for cfg in RELAYS.values():
        kind = ROLE_TARGET.get(cfg.get("role"))
        unit = cfg.get("unit") or ""
        if kind is None or not unit.startswith("pot_"):
            continue
        try:
            targets.add(Target(kind, int(unit.split("_", 1)[1])))
        except ValueError:
            continue
    return sorted(targets)


async def _mode_menu(state: RuntimeState) -> None:
    """
    Set a unit's control mode locally.

    The same operation the server performs with ``boiler.set_mode``: an
    operator standing in the boiler room should not need the cloud to take a
    boiler off the schedule.
    """
    targets = _controllable_targets()
    if not targets:
        await state.echo("\n[menu] No boilers or pumps in the device mapping.\n")
        return

    rc = state.relay_controller
    modes = await state.get_modes()
    blocks = await state.get_limit_blocks()

    await state.echo("\n[menu] Unit modes:")
    for position, target in enumerate(targets, start=1):
        relay_id = relay_for_target(target)
        relay_state = (
            ("ON" if rc.get_state(relay_id) else "OFF")
            if rc is not None and relay_id is not None
            else "unknown"
        )
        mode = modes.get(target, "automatic")
        cut = f"  [CUT: {blocks[target]}]" if target in blocks else ""
        await state.echo(
            f"  {position}) {str(target):<10} {mode:<10} relay {relay_id} {relay_state}{cut}"
        )

    raw = await _prompt("Which unit? (empty = back): ")
    if not raw:
        await state.echo("")
        return
    try:
        target = targets[int(raw) - 1]
    except (ValueError, IndexError):
        await state.echo("[menu] Invalid selection.\n")
        return

    current = modes.get(target, "automatic")
    answer = await _prompt(
        f"  {target} is {current}. Set to 1) automatic or 2) manual? (empty = back): "
    )
    choice = answer.strip().lower()
    if not choice:
        await state.echo("")
        return
    if choice in ("1", "a", "auto", "automatic"):
        mode = "automatic"
    elif choice in ("2", "m", "man", "manual"):
        mode = "manual"
    else:
        await state.echo("[menu] Invalid mode.\n")
        return

    if mode == current:
        await state.echo(f"[menu] {target} is already {mode}.\n")
        return

    await state.set_mode(target, mode)

    if mode == "automatic":
        # Hand the unit back to the schedule now rather than leaving it where
        # the operator left it until the next start/end boundary.
        schedule_runner.forget(target)
        await schedule_runner.evaluate(state)
        relay_id = relay_for_target(target)
        now_on = rc.get_state(relay_id) if rc is not None and relay_id is not None else None
        await state.echo(
            f"[menu] {target} -> automatic; the schedule now drives it"
            + (f" (relay {relay_id} {'ON' if now_on else 'OFF'})" if now_on is not None else "")
            + "\n"
        )
    else:
        await state.echo(
            f"[menu] {target} -> manual; the schedule will leave it alone "
            "until you set it back.\n"
        )

    await state.log(f"[menu] {target} set to {mode} by operator")
    # Modes appear in telemetry, so report this without waiting for the cycle.
    await state.notify_state_change(f"{target} mode {mode} (operator)")

    # Telemetry settles reported_mode; this is what is meant to settle
    # desired_mode. Fire and forget — see report_unit_mode.
    from ws_client import report_unit_mode

    if await report_unit_mode(state, target, mode):
        await state.echo("[menu] Reported to the server.\n")
    else:
        await state.echo(
            "[menu] Offline — the mode will be reported when the device "
            "reconnects.\n"
        )


# ---------------------------------------------------------------------------
# Schedule editing
# ---------------------------------------------------------------------------
#
# The operator edits the raw schedule document and it is handed to
# parse_schedule, so a programme built here is checked exactly like one the
# server pushed. See schedule_editor for why a local edit keeps the published
# version number and why a publish supersedes it.


def _edit_token() -> tuple[int, int]:
    """
    What the running schedule is, for detecting a change mid-edit.

    Each prompt is a blocking ``input()`` that can sit there for minutes, and a
    ``schedule.apply`` may land in that time. Applying an edit built on the old
    document would silently undo the push, so the token is re-checked first.
    """
    return schedule_runner.server_version, schedule_runner.local_revision


def _document_for_edit() -> dict:
    document = schedule_runner.document
    if document is not None:
        return document
    # Nothing published or cached yet — a room being commissioned before its
    # schedule exists. Version 0 means the first publish supersedes this.
    return blank_document()


def _schedule_today() -> datetime.date:
    """Today in the schedule's timezone, which is what exceptions match on."""
    schedule = schedule_runner.schedule
    if schedule is None:
        return datetime.date.today()
    return schedule.local_time().date()


async def _schedule_status(state: RuntimeState) -> None:
    schedule = schedule_runner.schedule
    if schedule is None:
        await state.echo(
            "\n[menu] No schedule yet — adding a rule starts one on this device."
        )
        return

    await state.echo(f"\n[menu] Schedule v{schedule.version}", )
    if schedule_runner.is_locally_modified:
        await state.echo(
            f"        locally edited (revision {schedule_runner.local_revision}, "
            f"on top of published v{schedule_runner.server_version})"
        )
    for line in schedule_runner.describe_weekly_rules():
        await state.echo(f"  {line}")
    for line in schedule_runner.describe_exceptions():
        await state.echo(f"  {line}")


async def _select_schedule_targets(state: RuntimeState) -> list[Target] | None:
    """Pick the boilers and pumps a rule applies to. None means cancelled."""
    targets = _controllable_targets()
    if not targets:
        await state.echo("[menu] No boilers or pumps in the device mapping.\n")
        return None

    await state.echo("  Targets:")
    for position, target in enumerate(targets, start=1):
        await state.echo(f"    {position}) {str(target):<10} relay {relay_for_target(target)}")

    raw = await _prompt("  Which? (e.g. 1,3 or 13; 0 = all, empty = cancel): ")
    if not raw:
        return None

    text = raw.replace(" ", "").lower()
    if text in ("all", "0"):
        return targets

    parts = [part for part in text.split(",") if part]

    # A run of digits with no separators — "13" for units 1 and 3 — because the
    # keypad's comma is one more key to find in a boiler room. Only when the
    # number is not itself a position on the list, so an installation with
    # thirteen units still reads "13" as the thirteenth.
    if len(parts) == 1 and parts[0].isdigit() and len(parts[0]) > 1:
        if not 1 <= int(parts[0]) <= len(targets):
            parts = list(parts[0])

    chosen: list[Target] = []
    for part in parts:
        try:
            position = int(part)
        except ValueError:
            await state.echo(f"[menu] {part!r} is not a number from the list.\n")
            return None
        if not 1 <= position <= len(targets):
            await state.echo(f"[menu] There is no target {position}.\n")
            return None
        if targets[position - 1] not in chosen:
            chosen.append(targets[position - 1])

    return chosen or None


async def _apply_local_edit(
    state: RuntimeState,
    document: dict,
    token: tuple[int, int],
    summary: str,
) -> None:
    """Validate an edited document, adopt it, and say plainly what it now means."""
    if _edit_token() != token:
        await state.echo(
            "[menu] The schedule changed while you were editing it — most likely "
            "the server published one. Nothing was saved; take another look and "
            "try again.\n"
        )
        return

    try:
        schedule = await schedule_runner.apply_local_document(document, state)
    except ScheduleError as exc:
        # Same validator the server's documents go through, so this is the same
        # message a bad push would produce.
        await state.echo(f"[menu] Rejected: {exc}\n")
        return

    await state.echo(f"[menu] Schedule updated — {summary}.")
    if not schedule_runner.local_persisted:
        await state.echo(
            "[menu] WARNING: it could not be written to disk, so it will not "
            "survive a restart."
        )
    await state.echo(
        f"[menu] Now running a locally edited v{schedule.version} "
        f"(revision {schedule_runner.local_revision}). The server's next publish "
        "replaces it."
    )
    for line in schedule_runner.describe_now():
        await state.echo(f"  {line}")
    await state.echo("")

    await state.log(
        f"[menu] Operator edited the schedule: {summary} "
        f"(local revision {schedule_runner.local_revision}, "
        f"published v{schedule_runner.server_version})",
        level=logging.WARNING,
    )

    # Relay changes the edit caused are already reported by evaluate(); this
    # tells a dashboard the *programme* changed even when nothing switched yet.
    from ws_client import publish_schedule_update, push_device_state

    await push_device_state(state)

    # Offer it upstream. DEVICE.md's schedule.update makes the edit the room's
    # real schedule rather than a local override, so this is what turns "the
    # cloud disagrees with the boiler room" back into "they agree".
    await state.echo("[menu] Publishing to the server ...")
    ack = await publish_schedule_update(state)

    if ack and ack.get("ok"):
        await state.echo(
            f"[menu] Published — the server created schedule "
            f"v{ack.get('schedule_version')}. This is now the room's schedule, "
            "not a local edit.\n"
        )
    elif ack:
        await state.echo(
            f"[menu] The server refused it: {ack.get('error') or ack}\n"
            "[menu] The change is still running here, and will be offered "
            "again on the next connection.\n"
        )
    else:
        await state.echo(
            "[menu] No answer from the server — the change is running here and "
            "will be published when the device reconnects.\n"
        )


async def _add_weekly_rule(state: RuntimeState) -> None:
    token = _edit_token()
    document = _document_for_edit()

    targets = await _select_schedule_targets(state)
    if not targets:
        await state.echo("[menu] Cancelled.\n")
        return

    try:
        days = parse_days(
            await _prompt("  Days? (1=Mon .. 7=Sun, e.g. 135; 0 = every day): ")
        )
        start = parse_time_text(await _prompt("  Start time (HHMM): "), "start")
        end = parse_time_text(await _prompt("  End time (HHMM): "), "end")
        turn_on = parse_state_text(
            await _prompt("  Switch them on or off in that window? [1 = on, 0 = off]: ")
        )
        edited = add_weekly_rule(
            document,
            days=days,
            start=start,
            end=end,
            state=turn_on,
            targets=targets,
        )
    except ScheduleEditError as exc:
        await state.echo(f"[menu] {exc}\n")
        return

    await _apply_local_edit(
        state,
        edited,
        token,
        f"{start}-{end} {','.join(d[:3] for d in days)} -> "
        f"{'ON' if turn_on else 'OFF'} for {', '.join(str(t) for t in targets)}",
    )


async def _remove_weekly_rule(state: RuntimeState) -> None:
    token = _edit_token()
    document = _document_for_edit()

    await state.echo("")
    for line in schedule_runner.describe_weekly_rules():
        await state.echo(f"  {line}")

    raw = await _prompt("  Remove which rule? (empty = back): ")
    if not raw:
        await state.echo("")
        return

    try:
        edited = remove_weekly_rule(document, int(raw))
    except ValueError as exc:
        # Covers both a non-numeric answer and a position that does not exist.
        message = exc if isinstance(exc, ScheduleEditError) else f"{raw!r} is not a rule number"
        await state.echo(f"[menu] {message}\n")
        return

    await _apply_local_edit(state, edited, token, f"removed weekly rule {int(raw)}")


async def _add_exception(state: RuntimeState) -> None:
    token = _edit_token()
    document = _document_for_edit()

    targets = await _select_schedule_targets(state)
    if not targets:
        await state.echo("[menu] Cancelled.\n")
        return

    try:
        date = parse_date_text(
            await _prompt("  Date? (0 = today, 1 = tomorrow, or MMDD / YYYYMMDD): "),
            today=_schedule_today(),
        )
        turn_on = parse_state_text(
            await _prompt("  Force them on or off? [1 = on, 0 = off]: ")
        )

        all_day = not _is_no(await _prompt("  All day? [0 = no, anything else = yes]: "))
        start = end = None
        if not all_day:
            start = parse_time_text(await _prompt("  Start time (HHMM): "), "start")
            end = parse_time_text(await _prompt("  End time (HHMM): "), "end")

        reason = (await _prompt("  Reason (optional): ")).strip()

        edited = add_exception(
            document,
            date=date,
            state=turn_on,
            targets=targets,
            all_day=all_day,
            start=start,
            end=end,
            reason=reason,
        )
    except ScheduleEditError as exc:
        await state.echo(f"[menu] {exc}\n")
        return

    window = "all day" if all_day else f"{start}-{end}"
    await _apply_local_edit(
        state,
        edited,
        token,
        f"exception {date} {window} -> {'ON' if turn_on else 'OFF'} "
        f"for {', '.join(str(t) for t in targets)}",
    )


async def _remove_exception(state: RuntimeState) -> None:
    token = _edit_token()
    document = _document_for_edit()

    await state.echo("")
    for line in schedule_runner.describe_exceptions():
        await state.echo(f"  {line}")

    raw = await _prompt("  Remove which exception? (empty = back): ")
    if not raw:
        await state.echo("")
        return

    try:
        edited = remove_exception(document, int(raw))
    except ValueError as exc:
        message = (
            exc if isinstance(exc, ScheduleEditError) else f"{raw!r} is not an exception number"
        )
        await state.echo(f"[menu] {message}\n")
        return

    await _apply_local_edit(state, edited, token, f"removed exception {int(raw)}")


async def _discard_local_edits(state: RuntimeState) -> None:
    if not schedule_runner.is_locally_modified:
        await state.echo("\n[menu] There are no local edits to discard.\n")
        return

    answer = await _prompt(
        "\n  Discard local edits and go back to the published schedule? "
        "[1 or y = yes]: "
    )
    if not _is_yes(answer):
        await state.echo("[menu] Cancelled.\n")
        return

    schedule = await schedule_runner.revert_local(state)
    if schedule is None:
        await state.echo(
            "[menu] Local edits discarded. Nothing has ever been published to "
            "this device, so no programme is driving the relays — they stay "
            "where they are until the server sends one.\n"
        )
    else:
        await state.echo(f"[menu] Back to the published schedule v{schedule.version}.\n")
        for line in schedule_runner.describe_now():
            await state.echo(f"  {line}")
        await state.echo("")

    await state.log(
        "[menu] Operator discarded the local schedule edits", level=logging.WARNING
    )

    from ws_client import push_device_state

    await push_device_state(state)


async def _schedule_editor_menu(state: RuntimeState) -> None:
    """
    Change the heating programme from the device.

    The same reasoning as option 7: an operator standing in the boiler room
    should not need the cloud to change how the boilers run. Edits hold until
    the server publishes a schedule, which then wins.
    """
    while not state.shutdown.is_set():
        await _schedule_status(state)

        try:
            choice = await _prompt(SCHEDULE_MENU)
        except EOFError:
            state.shutdown.set()
            return

        if choice == "1":
            await _add_weekly_rule(state)
        elif choice == "2":
            await _remove_weekly_rule(state)
        elif choice == "3":
            await _add_exception(state)
        elif choice == "4":
            await _remove_exception(state)
        elif choice == "5":
            await _discard_local_edits(state)
        elif choice in ("0", ""):
            await state.echo("")
            return
        else:
            await state.echo(f"\n[menu] Unknown option: {choice!r}\n")


# ---------------------------------------------------------------------------
# Temperature limits
# ---------------------------------------------------------------------------
#
# Mirrors the schedule editor: the operator edits the raw config document and
# it is handed to parse_config, a local edit keeps the published version
# number, and the server's next publish supersedes it. See config_editor.


def _limits_edit_token() -> tuple[int, int]:
    """What the running config is, for detecting a push mid-edit."""
    return config_store.server_version, config_store.local_revision


def _limits_document_for_edit() -> dict:
    document = config_store.document
    if document is not None:
        return document
    # Nothing published or cached yet — a room being commissioned. Version 0
    # means the server's first publish supersedes these limits.
    return blank_config_document()


async def _boiler_targets_with_relays() -> list[Target]:
    return [t for t in _controllable_targets() if t.type == "boiler"]


async def _show_temperature_status(state: RuntimeState) -> None:
    """Every boiler's setpoint and the cut-out that follows from it."""
    setpoints = await state.get_setpoints()
    config = await state.get_device_config()

    await state.echo("\n[menu] Boiler temperatures:")
    for line in limit_guard.describe(config, setpoints):
        await state.echo(f"  {line}")

    unpublished = await state.unpublished_setpoints()
    if unpublished:
        await state.echo(
            "  Not yet published to the server: "
            + ", ".join(f"boiler {i}" for i in unpublished)
            + " — will be sent on the next connection"
        )


async def _set_boiler_temperature(state: RuntimeState) -> None:
    targets = await _boiler_targets_with_relays()
    if not targets:
        await state.echo("\n[menu] No boilers in the device mapping.\n")
        return

    setpoints = await state.get_setpoints()
    await state.echo("")
    for position, target in enumerate(targets, start=1):
        entry = setpoints.get(target.index)
        shown = f"{entry.temperature_c:.1f} °C" if entry else "not set"
        await state.echo(f"    {position}) {str(target):<10} {shown}")

    raw = await _prompt("  Which boiler? (empty = back): ")
    if not raw:
        await state.echo("")
        return
    try:
        target = targets[int(raw) - 1]
        if int(raw) < 1:
            raise IndexError
    except (ValueError, IndexError):
        await state.echo("[menu] Invalid selection.\n")
        return

    answer = await _prompt(
        f"  Temperature for {target} in °C "
        f"({MIN_SETPOINT_C:g}–{MAX_SETPOINT_C:g}, empty = cancel): "
    )
    if not answer:
        await state.echo("[menu] Cancelled.\n")
        return

    try:
        temperature = validate_setpoint(answer.strip())
    except SetpointError as exc:
        await state.echo(f"[menu] {exc}\n")
        return

    await state.set_setpoint(target.index, temperature)
    await state.log(
        f"[menu] Operator set {target} temperature to {temperature:.1f} °C",
        level=logging.WARNING,
    )
    await _after_temperature_change(
        state, target.index, temperature, f"{target} set to {temperature:.1f} °C"
    )


async def _clear_boiler_temperature(state: RuntimeState) -> None:
    setpoints = await state.get_setpoints()
    if not setpoints:
        await state.echo("\n[menu] No boiler has its own temperature set.\n")
        return

    indexes = sorted(setpoints)
    await state.echo("")
    for position, index in enumerate(indexes, start=1):
        await state.echo(
            f"    {position}) boiler {index}   {setpoints[index].temperature_c:.1f} °C"
        )

    raw = await _prompt("  Clear which? (empty = back): ")
    if not raw:
        await state.echo("")
        return
    try:
        position = int(raw)
        if not 1 <= position <= len(indexes):
            raise IndexError
    except (ValueError, IndexError):
        await state.echo("[menu] Invalid selection.\n")
        return

    index = indexes[position - 1]
    await state.clear_setpoint(index)
    await state.log(
        f"[menu] Operator cleared boiler {index}'s temperature — "
        "it now follows the device-wide limit",
        level=logging.WARNING,
    )
    await state.echo(
        f"[menu] Boiler {index} now follows the device-wide limit.\n"
    )
    # There is no protocol message for "no setpoint", so the server keeps the
    # last one it was told. Telemetry stops carrying setpoint_c, and the cut-out
    # here follows the device limit again — said plainly rather than implied.
    await state.echo(
        "[menu] Note: the server keeps the last temperature it was given; "
        "this only changes what this device holds the boiler at.\n"
    )
    for line in limit_guard.describe(
        await state.get_device_config(), await state.get_setpoints()
    ):
        await state.echo(f"  {line}")
    await state.echo("")

    from ws_client import push_device_state

    await push_device_state(state)


async def _after_temperature_change(
    state: RuntimeState,
    index: int,
    temperature: float,
    summary: str,
) -> None:
    """Show the new cut-out, then offer the setpoint upstream."""
    await state.echo(f"[menu] {summary}.")
    for line in limit_guard.describe(
        await state.get_device_config(), await state.get_setpoints()
    ):
        await state.echo(f"  {line}")

    from ws_client import publish_boiler_temperature, push_device_state

    await push_device_state(state)

    await state.echo("[menu] Publishing to the server ...")
    ack = await publish_boiler_temperature(state, index, temperature)

    if ack and ack.get("ok"):
        await state.echo(
            f"[menu] Published — the server has boiler {index} at "
            f"{ack.get('temperature_c')} °C, and has told the room's other "
            "devices.\n"
        )
    elif ack:
        await state.echo(
            f"[menu] The server refused it: {ack.get('error') or ack}\n"
            "[menu] The temperature is still in force here and will be offered "
            "again on the next connection.\n"
        )
    else:
        await state.echo(
            "[menu] No answer from the server — the temperature is in force "
            "here and will be published when the device reconnects.\n"
        )


async def _temperature_menu(state: RuntimeState) -> None:
    """
    Set each boiler's target temperature.

    Per unit, because that is what DEVICE.md models: every boiler has its own
    ``desired_temperature_c``, and one circuit may want 55 °C while another
    wants 75. The device-wide config limits sit underneath as the fallback for
    a boiler with no temperature of its own.
    """
    while not state.shutdown.is_set():
        await _show_temperature_status(state)

        try:
            choice = await _prompt(TEMPERATURE_MENU)
        except EOFError:
            state.shutdown.set()
            return

        if choice == "1":
            await _set_boiler_temperature(state)
        elif choice == "2":
            await _clear_boiler_temperature(state)
        elif choice == "3":
            await _limits_menu(state)
        elif choice in ("0", ""):
            await state.echo("")
            return
        else:
            await state.echo(f"\n[menu] Unknown option: {choice!r}\n")


async def _show_limits_status(state: RuntimeState) -> None:
    document = config_store.document
    version = config_store.server_version

    if document is None:
        await state.echo(
            "\n[menu] No config yet — setting a limit starts one on this device."
        )
    else:
        await state.echo(f"\n[menu] Limits (published config v{version})")
        if config_store.is_locally_modified:
            await state.echo(
                f"        locally edited (revision {config_store.local_revision}, "
                f"on top of published v{version})"
            )

    for line in describe_limits(document):
        await state.echo(line)

    # The numbers that actually matter: what each boiler cuts and restores at,
    # which is not the configured value for a single-probe unit.
    await state.echo("")
    for line in limit_guard.describe(
        await state.get_device_config(), await state.get_setpoints()
    ):
        await state.echo(f"  {line}")


async def _apply_limit_edit(
    state: RuntimeState,
    document: dict,
    token: tuple[int, int],
    summary: str,
) -> None:
    if _limits_edit_token() != token:
        await state.echo(
            "[menu] The config changed while you were editing it — most likely "
            "the server published one. Nothing was saved; take another look and "
            "try again.\n"
        )
        return

    try:
        config = await config_store.apply_local(document, state)
    except ConfigError as exc:
        # Same validator the server's documents go through.
        await state.echo(f"[menu] Rejected: {exc}\n")
        return

    await state.echo(f"[menu] Limits updated — {summary}.")
    if not config_store.local_persisted:
        await state.echo(
            "[menu] WARNING: they could not be written to disk, so they will "
            "not survive a restart."
        )
    await state.echo(
        f"[menu] Now running locally edited limits on v{config.version} "
        f"(revision {config_store.local_revision}). The server's next publish "
        "replaces them."
    )
    # A limit change moves every boiler's cut-out, so show the result rather
    # than leaving the operator to work it out from the probe count.
    for line in limit_guard.describe(config, await state.get_setpoints()):
        await state.echo(f"  {line}")
    await state.echo("")

    await state.log(
        f"[menu] Operator changed a temperature limit: {summary} "
        f"(local revision {config_store.local_revision}, published v{config_store.server_version})",
        level=logging.WARNING,
    )

    from ws_client import push_device_state

    await push_device_state(state)


async def _change_limit(state: RuntimeState, field: str, label: str) -> None:
    token = _limits_edit_token()
    document = _limits_document_for_edit()
    current = (document.get("limits") or {}).get(field)
    shown = f"{float(current):.1f} °C" if isinstance(current, (int, float)) else "not set"

    raw = await _prompt(
        f"\n  {label} is {shown}.\n"
        f"  New value in °C ('-' or 'none' removes it, empty = cancel): "
    )
    if not raw:
        await state.echo("[menu] Cancelled.\n")
        return

    try:
        value = parse_temperature_text(raw, label)
        edited = set_limit(document, field, value)
    except ConfigEditError as exc:
        await state.echo(f"[menu] {exc}\n")
        return

    await _apply_limit_edit(
        state,
        edited,
        token,
        f"{label.lower()} {'removed' if value is None else f'{value:g} °C'}",
    )


async def _discard_local_limits(state: RuntimeState) -> None:
    if not config_store.is_locally_modified:
        await state.echo("\n[menu] There are no local limit edits to discard.\n")
        return

    answer = await _prompt(
        "\n  Discard local limits and go back to the published config? "
        "[1 or y = yes]: "
    )
    if not _is_yes(answer):
        await state.echo("[menu] Cancelled.\n")
        return

    config = await config_store.revert_local(state)
    if config is None:
        await state.echo(
            "[menu] Local limits discarded. Nothing has ever been published to "
            "this device, so there are now NO temperature limits and no "
            "over-temperature cut until the server sends one.\n"
        )
    else:
        await state.echo(f"[menu] Back to the published config v{config.version}.")
        for line in limit_guard.describe(config, await state.get_setpoints()):
            await state.echo(f"  {line}")
        await state.echo("")

    await state.log(
        "[menu] Operator discarded the local temperature limits", level=logging.WARNING
    )

    from ws_client import push_device_state

    await push_device_state(state)


async def _limits_menu(state: RuntimeState) -> None:
    """
    Change the temperature limits from the device.

    The same reasoning as options 7 and 9: an operator standing in the boiler
    room should not need the cloud to change how the boilers are protected.
    Edits hold until the server publishes a config, which then wins.
    """
    while not state.shutdown.is_set():
        await _show_limits_status(state)

        try:
            choice = await _prompt(LIMITS_MENU)
        except EOFError:
            state.shutdown.set()
            return

        if choice in ("1", "2", "3"):
            field, label = LIMIT_FIELDS[int(choice) - 1]
            await _change_limit(state, field, label)
        elif choice == "4":
            await _discard_local_limits(state)
        elif choice in ("0", ""):
            await state.echo("")
            return
        else:
            await state.echo(f"\n[menu] Unknown option: {choice!r}\n")


async def _show_device_record(state: RuntimeState) -> None:
    """Show the server's own record of this device, refreshing it on request."""
    await state.echo("")
    for line in describe_record(device_record_store.record):
        await state.echo(f"  {line}")

    answer = await _prompt("\n  Re-fetch from server? [1 or y = yes]: ")
    if not _is_yes(answer):
        await state.echo("")
        return

    await state.echo("  Fetching ...")
    try:
        record, payload = await fetch_device_record()
    except Exception as exc:
        await state.echo(f"  Fetch failed: {exc}\n")
        return

    device_record_store.set_record(record)
    await save_cached_device_record(payload)
    await state.log(f"[device] Record refreshed from the control menu ({record.public_id})")

    await state.echo("")
    for line in describe_record(record):
        await state.echo(f"  {line}")
    await state.echo("")


async def _handle_choice(state: RuntimeState, choice: str) -> None:
    if choice == "1":
        await _show_last_readings(state)
    elif choice == "2":
        await _show_mapping(state)
    elif choice == "3":
        await _show_app_config(state)
    elif choice == "4":
        await _show_schedule(state)
    elif choice == "5":
        await _show_device_record(state)
    elif choice == "6":
        await _relay_menu(state)
    elif choice == "7":
        await _mode_menu(state)
    elif choice == "8":
        await _reload_mapping(state)
    elif choice == "9":
        await _schedule_editor_menu(state)
    elif choice == "10":
        await _temperature_menu(state)
    elif choice == "0":
        await state.echo("\n[menu] Shutting down ...")
        state.shutdown.set()
    else:
        await state.echo(f"\n[menu] Unknown option: {choice!r}\n")


def menu_enabled(device=None) -> bool:
    """
    Whether to offer the interactive menu.

    A keyboard needs a terminal: under systemd there is none, ``input()`` would
    raise EOFError immediately, the menu would treat that as "operator chose
    quit", and the service would exit and be restarted forever.

    A keypad needs nothing — it *is* the terminal, and the installed device is
    precisely where there is no TTY and the keypad is the only way in. So the
    device says whether it depends on one. BOILERROOM_MENU=on/off overrides.
    """
    override = os.environ.get("BOILERROOM_MENU", "").strip().lower()
    if override in ("on", "1", "true", "yes"):
        return True
    if override in ("off", "0", "false", "no"):
        return False

    if device is not None and not getattr(device, "needs_tty", True):
        return True

    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


async def _start_input_device(state: RuntimeState):
    """
    Bring up whatever the menu reads from, falling back to the keyboard.

    A keypad that will not start — miswired, pins already claimed, no
    permission — must not take the agent down with it: the boilers are running
    a heating programme and the server can still drive them. It costs the
    operator the local menu, which is worth saying loudly and nothing more.
    """
    from mock_keypad import MockKeypad

    device = getattr(state, "keypad", None) or MockKeypad()

    try:
        await device.start()
        return device
    except Exception as exc:
        await state.log(
            f"[menu] The {device.name} could not be started: {exc}",
            level=logging.ERROR,
        )

    if isinstance(device, MockKeypad):
        return device

    await state.log(
        "[menu] Falling back to the keyboard — nothing on this device can be "
        "controlled by hand until the keypad is fixed",
        level=logging.WARNING,
    )
    fallback = MockKeypad()
    await fallback.start()
    return fallback


async def run_control_menu(state: RuntimeState) -> None:
    device = await _start_input_device(state)
    set_input_device(device)

    if not menu_enabled(device):
        await state.log(
            "[menu] No terminal attached — control menu disabled "
            "(set BOILERROOM_MENU=on to force it)"
        )
        await device.close()
        await state.shutdown.wait()
        return

    try:
        await _run_menu_loop(state, device)
    finally:
        await device.close()


async def _run_menu_loop(state: RuntimeState, device) -> None:
    for line in getattr(device, "describe", list)():
        await state.echo(line)

    await state.echo(
        f"Control menu ready on the {device.name} — enter a number "
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

        try:
            await _handle_choice(state, choice)
        except EOFError:
            # The input device went away part-way through a submenu — stdin
            # closed, or the keypad was shut down. Only the submenus that
            # happen to guard their own prompts used to survive it; the rest
            # took the whole agent down with them, which is a heating system
            # stopping because a terminal was closed.
            state.shutdown.set()
            break
