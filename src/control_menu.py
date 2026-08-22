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

The graphical display
---------------------
Where one is fitted, the same menu is shown on it instead: one highlighted row
at a time, moved with ``2`` and ``8``, opened with ``#`` and left with ``*``,
with a legend along the bottom saying so. The functions below did not have to
change for it. Two things carry them across:

  * the option tables are the menu, and both faces are built from them — the
    numbered text for a terminal, the selectable list for the panel, so the two
    cannot drift apart
  * everything written with ``state.echo`` is captured rather than printed, and
    shown a screenful at a time the moment something asks for input

Which means a screen with six rows and a screen with fifty show the same
things, and there is one implementation of what those things are.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import sys
from pathlib import Path

from auth import (
    API_BASE_URL,
    WS_BASE_URL,
    CredentialError,
    credentials_present,
    device_username,
    set_credentials,
    token_manager,
)
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
from display_font import DEGREE
from keypad_layout import CANCEL, ENTER, cap_for
from mapping_provider import DEFAULT_MAPPING_PATH, DEFAULT_SOURCE
from limits_guard import limit_guard
from runtime_state import RuntimeState
from screen import SCROLL_KEYS, Screen
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

# The same options as the blocks above, as (answer, short label). The terminal
# reads the block; the display builds a selectable list from these. Labels are
# written to fit twenty columns, which is what the panel has.
MAIN_ITEMS = (
    ("1", "Sensor readings"),
    ("2", "Device mapping"),
    ("3", "App configuration"),
    ("4", "Active schedule"),
    ("5", "Server record"),
    ("6", "Relay control"),
    ("7", "Unit modes"),
    ("8", "Reload mapping"),
    ("9", "Change schedule"),
    ("10", "Temperatures"),
    ("0", "Quit"),
)

TEMPERATURE_ITEMS = (
    ("1", "Set boiler temp"),
    ("2", "Clear boiler temp"),
    ("3", "Safety limits"),
    ("0", "Back"),
)

LIMITS_ITEMS = (
    ("1", "Max water temp"),
    ("2", "Min water temp"),
    ("3", "Max ambient temp"),
    ("4", "Discard local edits"),
    ("0", "Back"),
)

SCHEDULE_ITEMS = (
    ("1", "Add weekly rule"),
    ("2", "Remove weekly rule"),
    ("3", "Add date exception"),
    ("4", "Remove exception"),
    ("5", "Discard local edits"),
    ("0", "Back"),
)

# Returned when the operator pressed the back key rather than choosing
# anything. Distinct from "0" because on the main menu "0" is Quit, and a back
# key that stops the heating agent is not a back key.
BACK = "\x00back"


# The device answers are read from. A module-level handle, like the schedule
# and config stores, so the twenty-odd call sites of _prompt keep their
# signature instead of threading it through every menu function.
_input_device = None

# The panel those answers are shown on, or None where there is no display and
# the menu is a terminal one. Held here for the same reason.
_screen: Screen | None = None

# The state, for the handful of helpers below that need it and are called from
# places whose signature is fixed by twenty existing call sites.
_state: RuntimeState | None = None

# What the screen currently being drawn is about, used to title the pages that
# menu output is shown on. Set from the option label as each one is opened.
_context = "Menu"


def set_input_device(device) -> None:
    global _input_device
    _input_device = device


def input_device():
    """The active input device, defaulting to the keyboard."""
    if _input_device is None:
        from mock_keypad import MockKeypad

        set_input_device(MockKeypad())
    return _input_device


def set_screen(screen: Screen | None) -> None:
    global _screen
    _screen = screen


def screen() -> Screen | None:
    """The display's screens, or None when the menu is a terminal one."""
    return _screen


def _set_context(title: str) -> None:
    global _context
    _context = title


def _label_for(items: tuple[tuple[str, str], ...], answer: str, default: str) -> str:
    for value, label in items:
        if value == answer:
            return label
    return default


def _strip_tag(line: str) -> str:
    """Drop the ``[menu]`` prefix — on twenty columns it is four wasted words."""
    return line[7:] if line.startswith("[menu] ") else line


async def _flush_page(state: RuntimeState) -> None:
    """
    Show whatever the menu has written since the last screen, then clear it.

    Called before anything asks for input, which is what turns output written
    for a scrolling terminal into pages on a panel that does not scroll:
    everything a function echoed is on screen, in order, and paged through at
    the operator's speed rather than gone by the time they look up.
    """
    view = screen()
    if view is None:
        return

    lines = [_strip_tag(line) for line in state.take_echo()]

    # A page waits for a keypress. On the way out there is nobody to press one,
    # and the buffer would hold the agent open on a screen saying it is closing.
    if state.shutdown.is_set() or not any(line.strip() for line in lines):
        return

    await view.page(_context, lines)


async def _prompt(text: str, *, mask: bool = False) -> str:
    """
    Ask a question and read the answer.

    ``mask`` hides what is typed, which only the device password wants. It has
    no effect on a terminal, where the keyboard's own echo is doing the showing
    and there is no panel to hide it on.
    """
    view = screen()
    if view is None:
        return (await input_device().read_line(text)).strip()

    if _state is not None:
        await _flush_page(_state)
    return (await view.read_line(text, title=_context, mask=mask)).strip()


async def _notice(state: RuntimeState, title: str, lines: list[str]) -> None:
    """
    Put something on the panel and carry straight on.

    For the screens nobody should have to acknowledge — "checking with the
    server" while a login runs. ``_flush_page`` would stop and wait for a key,
    which is right for output an operator asked for and wrong for a thing that
    is about to replace itself.
    """
    view = screen()
    if view is None:
        for line in lines:
            await state.echo(line)
        return

    _set_context(title)
    state.take_echo()  # these are on the panel now, not queued behind it
    await view.splash(title, lines)


async def _message(state: RuntimeState, title: str, lines: list[str]) -> None:
    """Show something the operator has to read, and wait for them to move on."""
    view = screen()
    if view is None:
        for line in lines:
            await state.echo(line)
        return

    _set_context(title)
    state.take_echo()
    await view.page(title, lines)


async def _choose(
    state: RuntimeState,
    title: str,
    items: tuple[tuple[str, str], ...],
    text: str,
    *,
    hide_back: bool = True,
    legend: tuple[tuple[str, str], ...] | None = None,
) -> str:
    """
    Ask which option. Returns the answer the handlers already expect.

    On a terminal this is the numbered block and a typed number, unchanged. On
    the panel it is a selectable list — with the "Back" row left out, because
    the back key is right there on the keypad and a list that spends one of its
    six rows saying so is a list with five rows.
    """
    view = screen()
    if view is None:
        return await _prompt(text)

    await _flush_page(state)

    shown = [item for item in items if not (hide_back and item[0] == "0")]
    index = _last_choice.get(title, 0)
    chosen = await view.select(
        title,
        [label for _, label in shown],
        index=min(index, len(shown) - 1),
        legend=legend,
    )
    if chosen is None:
        return BACK

    _last_choice[title] = chosen
    return shown[chosen][0]


# Where each menu was left, so coming back from a submenu lands on the row it
# was opened from rather than at the top.
_last_choice: dict[str, int] = {}


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
    await state.echo(f"  Device username:  {device_username() or '(not set)'}")
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
        _set_context("Schedule")
        await _schedule_status(state)

        try:
            choice = await _choose(state, "Schedule", SCHEDULE_ITEMS, SCHEDULE_MENU)
        except EOFError:
            state.shutdown.set()
            return

        _set_context(_label_for(SCHEDULE_ITEMS, choice, "Schedule"))

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
        elif choice in ("0", "", BACK):
            await state.echo("")
            return
        else:
            await state.echo(f"\n[menu] Unknown option: {choice!r}\n")

        await _flush_page(state)


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
        _set_context("Temperatures")
        await _show_temperature_status(state)

        try:
            choice = await _choose(
                state, "Temperatures", TEMPERATURE_ITEMS, TEMPERATURE_MENU
            )
        except EOFError:
            state.shutdown.set()
            return

        _set_context(_label_for(TEMPERATURE_ITEMS, choice, "Temperatures"))

        if choice == "1":
            await _set_boiler_temperature(state)
        elif choice == "2":
            await _clear_boiler_temperature(state)
        elif choice == "3":
            await _limits_menu(state)
        elif choice in ("0", "", BACK):
            await state.echo("")
            return
        else:
            await state.echo(f"\n[menu] Unknown option: {choice!r}\n")

        await _flush_page(state)


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
        _set_context("Safety limits")
        await _show_limits_status(state)

        try:
            choice = await _choose(state, "Safety limits", LIMITS_ITEMS, LIMITS_MENU)
        except EOFError:
            state.shutdown.set()
            return

        _set_context(_label_for(LIMITS_ITEMS, choice, "Safety limits"))

        if choice in ("1", "2", "3"):
            field, label = LIMIT_FIELDS[int(choice) - 1]
            await _change_limit(state, field, label)
        elif choice == "4":
            await _discard_local_limits(state)
        elif choice in ("0", "", BACK):
            await state.echo("")
            return
        else:
            await state.echo(f"\n[menu] Unknown option: {choice!r}\n")

        await _flush_page(state)


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


# ---------------------------------------------------------------------------
# The graphical display
# ---------------------------------------------------------------------------


# On the main menu there is nothing to go back to, so the back key is given the
# screen an operator standing at the boiler wants most.
ROOT_LEGEND = (SCROLL_KEYS, (cap_for(ENTER), "Open"), (cap_for(CANCEL), "Status"))

# How often the status screen redraws itself when there is no keypad to ask for
# it. Matched to the sensor cadence: anything faster redraws the same numbers.
STATUS_REFRESH_SECONDS = 30.0


def _short_reason(reason: str) -> str:
    """A cut-out reason that fits twenty columns."""
    return {
        "water_over_temperature": "water too hot",
        "ambient_over_temperature": "room too hot",
    }.get(reason, reason.replace("_", " "))


def _role_reading(temperatures: dict, role: str) -> float | None:
    for sensor_id, value in temperatures.items():
        if value is None:
            continue
        if TEMPERATURE_SENSORS.get(sensor_id, {}).get("role") == role:
            return value
    return None


async def _status_lines(state: RuntimeState) -> list[str]:
    """
    What the room is doing, in twenty columns.

    The one screen worth designing around: it is what is on the panel when
    nobody is in a menu, and what an operator checks before touching anything.
    So it leads with the units — temperature, relay, and who is driving them —
    and a boiler held off by the limit guard says so where its mode would be,
    because that is the answer to "why is it not firing".
    """
    from limits_guard import _control_temperature

    snapshot = await state.get_snapshot()
    temperatures = snapshot["temperatures"]
    modes = await state.get_modes()
    blocks = await state.get_limit_blocks()
    setpoints = await state.get_setpoints()
    ws = await state.get_ws_status()
    relay_controller = state.relay_controller

    if ws["connected"]:
        link = "online"
    elif state.authenticated.is_set():
        link = "no socket"
    else:
        link = "offline"

    config_version, schedule_version = await state.get_active_versions()
    lines = [f"Link {link}", f"Cfg v{config_version}  Sched v{schedule_version}", ""]

    targets = _controllable_targets()
    if not targets:
        lines.append("No device mapping —")
        lines.append("waiting for the server")
        return lines

    for target in targets:
        relay_id = relay_for_target(target)
        tag = f"{target.type[0].upper()}{target.index}"

        temperature = (
            _control_temperature(f"pot_{target.index}", temperatures)
            if target.type == "boiler"
            else None
        )
        reading = f"{temperature:5.1f}{DEGREE}C" if temperature is not None else " " * 7

        if relay_controller is not None and relay_id is not None:
            switch = "ON " if relay_controller.get_state(relay_id) else "OFF"
        else:
            switch = "  ?"

        if target in blocks:
            mode = "CUT"
        else:
            mode = "MAN" if modes.get(target, "automatic") == "manual" else "AUT"

        lines.append(f"{tag:<3}{reading} {switch} {mode}")

    for target, reason in sorted(blocks.items(), key=str):
        lines.append(f"  {target} cut:")
        lines.append(f"    {_short_reason(reason)}")

    for index, entry in sorted(setpoints.items()):
        published = "" if entry.published else " (unsent)"
        lines.append(f"Target B{index} {entry.temperature_c:.1f}{DEGREE}C{published}")

    lines.append("")
    inside = _role_reading(temperatures, "environment_inside")
    outside = _role_reading(temperatures, "environment_outside")
    if inside is not None:
        lines.append(f"Inside  {inside:5.1f}{DEGREE}C")
    if outside is not None:
        lines.append(f"Outside {outside:5.1f}{DEGREE}C")

    for sensor_id, value in sorted(snapshot["gas"].items()):
        lines.append(f"Gas {sensor_id}   {value}")

    read_at = snapshot["read_at"]
    lines.append(f"Read {read_at:%H:%M:%S}Z" if read_at else "No readings yet")

    return lines


async def _show_status(state: RuntimeState) -> None:
    view = screen()
    if view is None:
        return
    _set_context("Status")
    await view.page("Status", await _status_lines(state))


async def _confirm_quit(state: RuntimeState) -> bool:
    """
    Check before stopping the agent.

    Not paranoia about one extra keypress: the selection wraps, so "Quit" is
    one press *up* from the top of the main menu. Nothing else on this device
    is one slip away from stopping the heating.
    """
    view = screen()
    if view is None:
        return True  # the terminal menu needs a typed "0", which is confirmation enough

    chosen = await view.select(
        "Stop the agent?",
        ["No, keep running", "Yes, stop it"],
        index=0,
    )
    return chosen == 1


async def _start_screen(state: RuntimeState, device) -> Screen | None:
    """
    Bring up the panel, tolerating not having one.

    Same rule as the keypad: a display that will not start costs the operator a
    screen and nothing else. The boilers are running a programme and the server
    can still drive them, so this logs loudly and hands the menu back to the
    terminal.
    """
    display = getattr(state, "display", None)
    if display is None or not getattr(display, "available", True):
        return None

    if not hasattr(device, "read_key"):
        # These screens move a selection; they need the keys one at a time.
        await state.log(
            f"[menu] The {device.name} cannot report single keys — the display "
            "stays dark and the menu stays on the terminal",
            level=logging.WARNING,
        )
        return None

    try:
        await display.start()
    except Exception as exc:
        await state.log(
            f"[menu] The {getattr(display, 'name', 'display')} could not be "
            f"started: {exc} — the menu falls back to the terminal",
            level=logging.ERROR,
        )
        return None

    for line in getattr(display, "describe", list)():
        await state.log(f"[menu] {line}")

    view = Screen(display, device, echo=state.write)
    set_screen(view)
    await view.splash("Boiler room", ["", "  Starting up ...", ""])
    return view


async def _stop_screen(state: RuntimeState) -> None:
    view = screen()
    set_screen(None)
    state.stop_echo_capture()
    if view is None:
        return

    try:
        # Leave something true on the glass. A menu frozen where the operator
        # last left it reads as a working device.
        await view.splash("Boiler room", ["", "  Agent stopped.", ""])
        await view.display.close()
    except Exception as exc:
        await state.log(f"[menu] Display shutdown: {exc}", level=logging.DEBUG)


async def _run_status_display(state: RuntimeState, view: Screen) -> None:
    """
    Hold the status screen up when there is no way to answer a menu.

    The case this is for: a device whose keypad would not start. It still has a
    panel, and an operator in front of it should see temperatures rather than
    nothing. Longer status pages rotate a screenful at a time, since there is
    no key to scroll them with.
    """
    offset = 0

    while not state.shutdown.is_set():
        try:
            lines = await _status_lines(state)
            window = lines[offset : offset + 6] or lines[:6]
            await view.splash(
                "Status",
                window,
                legend=((cap_for(ENTER), "no keypad fitted"),),
            )
        except Exception as exc:
            await state.log(
                f"[menu] Could not draw the status screen: {exc}",
                level=logging.WARNING,
            )
            return

        offset = offset + 6 if offset + 6 < len(lines) else 0

        try:
            await asyncio.wait_for(state.shutdown.wait(), timeout=STATUS_REFRESH_SECONDS)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# First boot: the device's credentials
# ---------------------------------------------------------------------------
#
# A device is provisioned with a numeric username and password. Putting them in
# .env means a laptop, an SD card reader or an SSH session at the point of
# installation; typing them at the panel means neither. So a device that has
# none asks for them, once, and keeps what it is given.
#
# Two rules make that safe to do at a keypad:
#
#   * nothing is written to .env until a login has actually succeeded with it,
#     because an installer eventually mistypes a digit and a device that cached
#     the typo is one nobody at the panel could correct
#   * a refusal is told apart from an unreachable server, so a typo asks again
#     and an outage does not


# How long to wait for the server's verdict before letting the operator go. A
# login gets 30 s to time out, and the auth task's first retry is 10 s after
# that; past this it is the network, not the answer.
CREDENTIAL_CHECK_SECONDS = 45.0

# Whether this operator has already been shown the wizard, so declining it is
# not answered by asking again immediately.
_credentials_asked = False


def _credentials_wanted(state: RuntimeState) -> bool:
    """Whether to put the credentials wizard in front of somebody now."""
    # credentials_needed means the auth task asked, which happens at first boot
    # and again whenever the server refuses something typed here. Checked
    # alongside the live state rather than instead of it: at boot the menu can
    # reach its loop before the auth task has had a chance to raise its hand.
    if state.credentials_needed.is_set():
        return True
    return not credentials_present() and not _credentials_asked


async def _credentials_wizard(state: RuntimeState) -> None:
    """
    Ask for the device's credentials and hand them to the auth task.

    Loops while the server keeps refusing what is typed, since that is exactly
    the case an installer needs to be able to correct on the spot. Leaves as
    soon as the credentials are accepted, or as soon as it is clear the server
    cannot be reached to ask — in which case they stay in memory, the agent
    keeps retrying in the background, and they are saved the moment one of
    those retries succeeds.
    """
    global _credentials_asked

    _credentials_asked = True

    # Every screen below is written to fit the panel's six rows, so each one is
    # a single key to move past rather than something to be paged through
    # before you are allowed to start typing.
    await _message(
        state,
        "Sign in",
        [
            "Not signed in yet.",
            "",
            "Type the username",
            "and password from",
            "provisioning.",
            "Digits only.",
        ],
    )

    while not state.shutdown.is_set():
        state.credentials_needed.clear()
        state.credentials_ready.clear()
        _set_context("Sign in")

        username = await _prompt("Device username: ")
        if not username:
            await _declined(state)
            return

        password = await _prompt("Device password: ", mask=True)
        if not password:
            await _declined(state)
            return

        try:
            set_credentials(username, password)
        except CredentialError as exc:
            await _message(
                state, "Sign in", ["Cannot use that:", "", str(exc)]
            )
            continue

        # The auth task is waiting on this, and it is the only thing here that
        # knows how to log in.
        state.credentials_ready.set()
        await _notice(state, "Sign in", ["", "  Checking with the", "  server ..."])

        outcome = await _credential_outcome(state)

        if outcome == "accepted":
            await state.log(f"[menu] Signed in as {username}, entered on the device")
            await _message(
                state,
                "Sign in",
                [
                    "Accepted.",
                    "",
                    "Saved here. You will",
                    "not be asked again.",
                ],
            )
            return

        if outcome == "rejected":
            await state.log(
                "[menu] The server refused the credentials entered on the device",
                level=logging.WARNING,
            )
            await _message(
                state,
                "Sign in",
                [
                    "Not accepted.",
                    "",
                    "Check the username",
                    "and password, then",
                    "try again.",
                ],
            )
            continue

        if outcome == "stopped":
            return

        await state.log(
            "[menu] Credentials entered on the device, but the server could not "
            "be reached to check them — retrying in the background",
            level=logging.WARNING,
        )
        await _message(
            state,
            "Sign in",
            [
                "No answer from the",
                "server yet.",
                "",
                "Kept and retried in",
                "the background;",
                "saved when it works.",
            ],
        )
        return


async def _declined(state: RuntimeState) -> None:
    await state.log(
        "[menu] Sign-in was cancelled — the device stays offline and runs from "
        "its cached schedule",
        level=logging.WARNING,
    )
    await _message(
        state,
        "Sign in",
        [
            "Left unsigned.",
            "",
            "The boilers keep to",
            "the cached schedule.",
            "Restart to be asked.",
        ],
    )


async def _credential_outcome(state: RuntimeState) -> str:
    """
    Wait for the auth task's verdict on what was just typed.

    ``accepted``, ``rejected``, ``stopped`` or ``unanswered`` — the last
    meaning the server could not be reached in time to say, which is not a
    reason to make somebody stand at the panel any longer.
    """
    waiters = {
        "accepted": asyncio.create_task(state.authenticated.wait()),
        "rejected": asyncio.create_task(state.credentials_needed.wait()),
        "stopped": asyncio.create_task(state.shutdown.wait()),
    }

    try:
        done, _ = await asyncio.wait(
            waiters.values(),
            timeout=CREDENTIAL_CHECK_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in waiters.values():
            if not task.done():
                task.cancel()

    # Checked in this order so a session that came up wins over anything else
    # that happened to land in the same moment.
    for name, task in waiters.items():
        if task in done:
            return name
    return "unanswered"


# ---------------------------------------------------------------------------
# The menu itself
# ---------------------------------------------------------------------------


async def run_control_menu(state: RuntimeState) -> None:
    global _state
    _state = state

    device = await _start_input_device(state)
    set_input_device(device)
    view = await _start_screen(state, device)

    if not menu_enabled(device) and not _credentials_wanted(state):
        await state.log(
            "[menu] No terminal attached — control menu disabled "
            "(set BOILERROOM_MENU=on to force it)"
        )
        await device.close()
        if view is None:
            await state.shutdown.wait()
            return
        try:
            await _run_status_display(state, view)
        finally:
            await _stop_screen(state)
        return

    try:
        await _run_menu_loop(state, device)
    finally:
        await _stop_screen(state)
        await device.close()


async def _run_menu_loop(state: RuntimeState, device) -> None:
    view = screen()

    for line in getattr(device, "describe", list)():
        await state.echo(line)

    await state.echo(
        f"Control menu ready on the {device.name} — enter a number "
        "(sensor polling continues in background).\n"
    )

    if view is not None:
        # From here the menu's output belongs to the panel: captured and shown
        # a screenful at a time, rather than printed into a journal nobody is
        # reading while the operator looks at six blank rows.
        state.capture_echo()

    # The panel opens on the status screen rather than on the menu: it is what
    # somebody walking up to the device wants to see, and the menu is one key
    # away from it. Shown from inside the loop so it is covered by the handler
    # below like every other screen.
    pending_status = view is not None

    while not state.shutdown.is_set():
        # One handler for the whole step, because every screen below reads
        # keys and any of them can find the input device gone — stdin closed,
        # or the keypad shut down. Guarding only some of them is how a heating
        # agent ends on a traceback because a terminal was closed: this way it
        # sets shutdown and leaves through main's ordinary path, which releases
        # the relays and closes the database on the way out.
        try:
            # Before anything else: a device nobody has signed in cannot talk
            # to the server at all, so it is the first thing to put in front of
            # whoever is standing here.
            if _credentials_wanted(state):
                await _credentials_wizard(state)
                continue

            if pending_status:
                pending_status = False
                await _show_status(state)
                continue

            _set_context("Main menu")
            choice = await _choose(
                state,
                "Main menu",
                MAIN_ITEMS,
                MENU,
                hide_back=False,
                legend=ROOT_LEGEND,
            )

            if state.shutdown.is_set():
                break

            if choice == BACK:
                # Nothing sits above the main menu, so the back key is spent on
                # the screen worth reaching in one press from anywhere.
                await _show_status(state)
                continue

            if choice == "0" and not await _confirm_quit(state):
                continue

            _set_context(_label_for(MAIN_ITEMS, choice, "Menu"))
            await _handle_choice(state, choice)
            await _flush_page(state)
        except EOFError:
            state.shutdown.set()
            break
