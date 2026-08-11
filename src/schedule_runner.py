"""
Schedule handling.

The server pushes ``schedule.apply`` (DEVICE.md) with a weekly rule set and
date-specific exceptions. This module parses that document, works out which
targets should be energised at a given moment, and drives the relays.

Evaluation is edge-triggered: a relay is switched only when the state the
schedule computes for it *changes*. A manual toggle from the control menu
therefore survives until the next schedule transition instead of being
reverted a few seconds later.

Precedence, from weakest to strongest:
  1. default off
  2. weekly rules, in document order (a later matching rule wins)
  3. exceptions for today's date

The programme can also be edited on the device itself (see schedule_editor).
Such an edit is held as an *override* on top of the last published document:
it drives the relays until the server publishes a schedule, at which point the
published one wins and the override is discarded.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from load_env import env_path
from config import RELAYS
from json_store import read_json, write_json

LogFn = Callable[..., Awaitable[None]]

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The last applied schedule is cached here so the device keeps running its
# heating programme when the server is unreachable at boot.
SCHEDULE_CACHE_PATH = env_path("BOILERROOM_SCHEDULE_CACHE", "schedule_cache.json")

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# schedule target type -> relay role in the device mapping
TARGET_ROLE = {
    "boiler": "pot",
    "pump": "pump",
}

# exception action -> desired state
EXCEPTION_ACTIONS = {
    "heat_off": False,
    "heat_on": True,
}


class ScheduleError(ValueError):
    """Raised when a schedule document cannot be parsed."""


@dataclass(frozen=True, order=True)
class Target:
    type: str
    index: int

    def __str__(self) -> str:
        return f"{self.type} {self.index}"


@dataclass(frozen=True)
class WeeklyRule:
    days: frozenset[int]
    start: datetime.time
    end: datetime.time
    state: bool
    targets: tuple[Target, ...]

    def matches(self, local: datetime.datetime) -> bool:
        now = local.time()
        today = local.weekday()

        if self.start <= self.end:
            return today in self.days and self.start <= now < self.end

        # Window wraps past midnight: the day list refers to the start day.
        yesterday = (today - 1) % 7
        return (today in self.days and now >= self.start) or (
            yesterday in self.days and now < self.end
        )


@dataclass(frozen=True)
class ScheduleException:
    exception_id: str
    date: datetime.date
    start: datetime.time | None
    end: datetime.time | None
    all_day: bool
    state: bool
    targets: tuple[Target, ...]
    reason: str

    def matches(self, local: datetime.datetime) -> bool:
        if local.date() != self.date:
            return False
        if self.all_day or (self.start is None and self.end is None):
            return True
        now = local.time()
        start = self.start or datetime.time.min
        end = self.end or datetime.time.max
        return start <= now < end


@dataclass(frozen=True)
class Schedule:
    version: int
    timezone_name: str
    tzinfo: datetime.tzinfo | None
    weekly_rules: tuple[WeeklyRule, ...]
    exceptions: tuple[ScheduleException, ...]

    def targets(self) -> set[Target]:
        found: set[Target] = set()
        for rule in self.weekly_rules:
            found.update(rule.targets)
        for exception in self.exceptions:
            found.update(exception.targets)
        return found

    def local_time(self, now: datetime.datetime | None = None) -> datetime.datetime:
        now = now or datetime.datetime.now(datetime.UTC)
        if now.tzinfo is None:
            now = now.astimezone()
        return now.astimezone(self.tzinfo) if self.tzinfo else now.astimezone()

    def desired_states(
        self,
        now: datetime.datetime | None = None,
    ) -> dict[Target, bool]:
        """Target -> should it be on, at ``now`` (default: right now)."""
        local = self.local_time(now)

        states: dict[Target, bool] = {target: False for target in self.targets()}

        for rule in self.weekly_rules:
            if rule.matches(local):
                for target in rule.targets:
                    states[target] = rule.state

        for exception in self.exceptions:
            if exception.matches(local):
                for target in exception.targets:
                    states[target] = exception.state

        return states


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_time(raw: Any, field: str) -> datetime.time:
    if not isinstance(raw, str):
        raise ScheduleError(f"{field}: expected a 'HH:MM' string, got {raw!r}")
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    raise ScheduleError(f"{field}: cannot parse time {raw!r}")


def _parse_date(raw: Any, field: str) -> datetime.date:
    if not isinstance(raw, str):
        raise ScheduleError(f"{field}: expected a 'YYYY-MM-DD' string, got {raw!r}")
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError as exc:
        raise ScheduleError(f"{field}: cannot parse date {raw!r}") from exc


def _parse_days(raw: Any, field: str) -> frozenset[int]:
    if not isinstance(raw, list) or not raw:
        raise ScheduleError(f"{field}: 'days' must be a non-empty list")
    days: set[int] = set()
    for day in raw:
        key = str(day).strip().lower()
        if key not in WEEKDAYS:
            raise ScheduleError(f"{field}: unknown day {day!r}")
        days.add(WEEKDAYS[key])
    return frozenset(days)


def _parse_state(raw: Any, field: str) -> bool:
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in ("on", "true", "1", "heat_on"):
        return True
    if value in ("off", "false", "0", "heat_off"):
        return False
    raise ScheduleError(f"{field}: unknown state {raw!r}")


def _parse_targets(raw: Any, field: str) -> tuple[Target, ...]:
    if not isinstance(raw, list) or not raw:
        raise ScheduleError(f"{field}: 'targets' must be a non-empty list")
    targets: list[Target] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ScheduleError(f"{field}: each target must be an object")
        target_type = str(entry.get("type", "")).strip().lower()
        if target_type not in TARGET_ROLE:
            raise ScheduleError(
                f"{field}: unknown target type {entry.get('type')!r}. "
                f"Known types: {sorted(TARGET_ROLE)}"
            )
        index = entry.get("index")
        if not isinstance(index, int):
            raise ScheduleError(f"{field}: target 'index' must be an integer")
        targets.append(Target(target_type, index))
    return tuple(targets)


def _parse_exception_targets(raw: Any, field: str) -> tuple[Target, ...]:
    """Exceptions carry ``target: {"boiler_indexes": [...], "pump_indexes": [...]}``."""
    if not isinstance(raw, dict):
        raise ScheduleError(f"{field}: 'target' must be an object")

    targets: list[Target] = []
    for key, target_type in (("boiler_indexes", "boiler"), ("pump_indexes", "pump")):
        indexes = raw.get(key) or []
        if not isinstance(indexes, list):
            raise ScheduleError(f"{field}: '{key}' must be a list")
        for index in indexes:
            if not isinstance(index, int):
                raise ScheduleError(f"{field}: '{key}' entries must be integers")
            targets.append(Target(target_type, index))

    if not targets:
        raise ScheduleError(f"{field}: no boiler_indexes/pump_indexes given")
    return tuple(targets)


def _load_timezone(name: str) -> datetime.tzinfo | None:
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        # No tz database on this host (common on Windows without `tzdata`).
        # Fall back to system local time; the caller logs the downgrade.
        return None


def parse_schedule(payload: dict[str, Any]) -> Schedule:
    """Validate and normalize a ``schedule.apply`` payload."""
    if not isinstance(payload, dict):
        raise ScheduleError("schedule payload must be an object")

    version = payload.get("schedule_version")
    if not isinstance(version, int):
        raise ScheduleError(f"'schedule_version' must be an integer, got {version!r}")

    timezone_name = str(payload.get("timezone") or "")

    raw_rules = payload.get("weekly_rules") or []
    if not isinstance(raw_rules, list):
        raise ScheduleError("'weekly_rules' must be a list")

    rules: list[WeeklyRule] = []
    for position, entry in enumerate(raw_rules):
        field = f"weekly_rules[{position}]"
        if not isinstance(entry, dict):
            raise ScheduleError(f"{field}: expected an object")
        rules.append(
            WeeklyRule(
                days=_parse_days(entry.get("days"), field),
                start=_parse_time(entry.get("start"), f"{field}.start"),
                end=_parse_time(entry.get("end"), f"{field}.end"),
                state=_parse_state(entry.get("state"), f"{field}.state"),
                targets=_parse_targets(entry.get("targets"), field),
            )
        )

    raw_exceptions = payload.get("exceptions") or []
    if not isinstance(raw_exceptions, list):
        raise ScheduleError("'exceptions' must be a list")

    exceptions: list[ScheduleException] = []
    for position, entry in enumerate(raw_exceptions):
        field = f"exceptions[{position}]"
        if not isinstance(entry, dict):
            raise ScheduleError(f"{field}: expected an object")

        action = str(entry.get("action", "")).strip().lower()
        if action not in EXCEPTION_ACTIONS:
            raise ScheduleError(
                f"{field}: unknown action {entry.get('action')!r}. "
                f"Known actions: {sorted(EXCEPTION_ACTIONS)}"
            )

        all_day = bool(entry.get("all_day"))
        start = entry.get("start")
        end = entry.get("end")

        exceptions.append(
            ScheduleException(
                exception_id=str(entry.get("exception_id") or f"exception-{position}"),
                date=_parse_date(entry.get("date"), f"{field}.date"),
                start=None if start is None else _parse_time(start, f"{field}.start"),
                end=None if end is None else _parse_time(end, f"{field}.end"),
                all_day=all_day,
                state=EXCEPTION_ACTIONS[action],
                targets=_parse_exception_targets(entry.get("target"), field),
                reason=str(entry.get("reason") or ""),
            )
        )

    return Schedule(
        version=version,
        timezone_name=timezone_name,
        tzinfo=_load_timezone(timezone_name),
        weekly_rules=tuple(rules),
        exceptions=tuple(exceptions),
    )


# ---------------------------------------------------------------------------
# On-disk cache
# ---------------------------------------------------------------------------
#
# The raw payload is stored rather than the parsed object, so a reload goes
# through exactly the same validation the server push does.


async def load_cached_schedule(
    path: Path | None = None,
) -> tuple[Schedule | None, dict[str, Any] | None, str | None]:
    """
    Load the last applied schedule from disk.

    Returns ``(schedule, document, error)``. A missing or unreadable cache
    yields ``(None, None, None)`` — that is a normal first boot, not a failure.

    The raw document comes back alongside the parsed one because it is what a
    local edit is applied to, and what discarding a local edit reverts to.
    """
    payload = await read_json(path or SCHEDULE_CACHE_PATH)
    if payload is None:
        return None, None, None

    try:
        return await asyncio.to_thread(parse_schedule, payload), payload, None
    except ScheduleError as exc:
        return None, None, str(exc)


async def save_cached_schedule(
    payload: dict[str, Any],
    path: Path | None = None,
) -> None:
    """Persist a schedule payload that was successfully applied."""
    await write_json(path or SCHEDULE_CACHE_PATH, payload)


# ---------------------------------------------------------------------------
# Applying to relays
# ---------------------------------------------------------------------------


def relay_for_target(target: Target) -> int | None:
    """Find the relay driving a schedule target, via the device mapping."""
    role = TARGET_ROLE.get(target.type)
    if role is None:
        return None
    unit = f"pot_{target.index}"
    for relay_id, cfg in sorted(RELAYS.items()):
        if cfg.get("role") == role and cfg.get("unit") == unit:
            return relay_id
    return None


class ScheduleRunner:
    """Holds the active schedule and switches relays when it says to."""

    def __init__(self) -> None:
        self._schedule: Schedule | None = None
        self._applied: dict[Target, bool] = {}
        self._unmapped_warned: set[Target] = set()

        # The raw document behind _schedule, kept so the device can edit its own
        # programme, and the last one the server published, kept so those edits
        # can be discarded and the published programme put back.
        self._document: dict[str, Any] | None = None
        self._server_document: dict[str, Any] | None = None

        # 0 when the running schedule is the server's. Counts local edits
        # otherwise, so an operator can see their changes are still in force.
        self._local_revision = 0
        # Whether the last local edit reached the card. An edit is already in
        # effect by the time this is false; it just will not survive a restart.
        self._local_persisted = True

    @property
    def schedule(self) -> Schedule | None:
        return self._schedule

    @property
    def document(self) -> dict[str, Any] | None:
        """A copy of the running document, safe for an editor to mutate."""
        return copy.deepcopy(self._document) if self._document is not None else None

    @property
    def is_locally_modified(self) -> bool:
        return self._local_revision > 0

    @property
    def local_revision(self) -> int:
        return self._local_revision

    @property
    def local_persisted(self) -> bool:
        return self._local_persisted

    @property
    def server_version(self) -> int:
        """Version of the last published schedule; 0 if none has arrived."""
        version = (self._server_document or {}).get("schedule_version")
        return version if isinstance(version, int) else 0

    def set_schedule(
        self,
        schedule: Schedule,
        document: dict[str, Any] | None = None,
        *,
        local_revision: int = 0,
    ) -> None:
        self._schedule = schedule
        self._document = copy.deepcopy(document) if document is not None else None
        self._local_revision = local_revision
        # Re-apply everything on the next evaluation, even if the computed
        # states match what the previous schedule had already set.
        self._applied = {}
        self._unmapped_warned = set()

    def set_server_schedule(self, schedule: Schedule, document: dict[str, Any]) -> None:
        """Adopt a published document — from a push, or from the cache at boot."""
        self._server_document = copy.deepcopy(document)
        self.set_schedule(schedule, document)

    def forget(self, target: Target) -> None:
        """Drop the cached state for a target so the schedule re-asserts it."""
        self._applied.pop(target, None)

    async def evaluate(
        self,
        state,
        *,
        now: datetime.datetime | None = None,
    ) -> list[tuple[Target, int, bool]]:
        """Switch any relay whose scheduled state changed. Returns what changed."""
        schedule = self._schedule
        if schedule is None:
            return []

        relay_controller = state.relay_controller
        if relay_controller is None:
            # Sensor loop has not built the hardware yet; try again next tick.
            return []

        changes: list[tuple[Target, int, bool]] = []

        for target, want_on in sorted(schedule.desired_states(now).items()):
            # A safety cut-out outranks the schedule entirely.
            if await state.is_limit_blocked(target):
                continue

            # A target switched to manual mode is driven by commands only.
            if await state.get_mode(target) == "manual":
                continue

            relay_id = relay_for_target(target)
            if relay_id is None:
                if target not in self._unmapped_warned:
                    self._unmapped_warned.add(target)
                    await state.log(
                        f"[schedule] No relay mapped for {target} — ignoring it"
                    )
                continue

            if self._applied.get(target) == want_on:
                continue

            if want_on:
                await relay_controller.turn_on(relay_id)
            else:
                await relay_controller.turn_off(relay_id)

            self._applied[target] = want_on
            changes.append((target, relay_id, want_on))

            name = RELAYS.get(relay_id, {}).get("name", f"Relay {relay_id}")
            await state.log(
                f"[schedule] {name} (relay {relay_id}) -> "
                f"{'ON' if want_on else 'OFF'} for {target}"
            )
            await state.notify_state_change(
                f"relay {relay_id} {'on' if want_on else 'off'} (schedule)"
            )

        return changes

    async def apply_document(
        self,
        payload: dict[str, Any],
        state,
    ) -> Schedule:
        """Parse a ``schedule.apply`` payload, store it, and drive the relays."""
        # parse_schedule reads the tz database from disk via ZoneInfo, so keep
        # it off the event loop like every other bit of blocking I/O here.
        schedule = await asyncio.to_thread(parse_schedule, payload)
        superseded = self._local_revision
        self.set_server_schedule(schedule, payload)
        await state.set_active_schedule_version(schedule.version)

        # A publish is an explicit act by whoever owns the room, so it outranks
        # anything edited on the device. Said out loud rather than done quietly:
        # an operator who set a programme by hand needs to know it is gone.
        if superseded:
            from schedule_editor import clear_local_schedule

            try:
                await clear_local_schedule()
            except Exception as exc:
                await state.log(
                    f"[schedule] Could not remove the local override file: {exc}",
                    level=logging.WARNING,
                )
            await state.log(
                f"[schedule] Published schedule v{schedule.version} supersedes "
                f"{superseded} local edit(s) — the device now runs the published "
                "programme",
                level=logging.WARNING,
            )

        if schedule.timezone_name and schedule.tzinfo is None:
            await state.log(
                f"[schedule] Timezone {schedule.timezone_name!r} unavailable on this "
                "host (install 'tzdata') — using system local time"
            )

        await state.log(
            f"[schedule] Applied v{schedule.version} "
            f"({len(schedule.weekly_rules)} weekly rule(s), "
            f"{len(schedule.exceptions)} exception(s), tz={schedule.timezone_name})"
        )

        await self.evaluate(state)
        return schedule

    # -- locally edited programme --------------------------------------------

    async def apply_local_document(
        self,
        document: dict[str, Any],
        state,
    ) -> Schedule:
        """
        Adopt a programme edited on the device.

        Validated by ``parse_schedule`` first, so a bad edit raises and the
        running schedule is left exactly as it was. The version is *not*
        bumped: it names the published document these edits sit on top of, and
        raising it would only make the server re-push and undo them.
        """
        schedule = await asyncio.to_thread(parse_schedule, document)

        revision = self._local_revision + 1
        self.set_schedule(schedule, document, local_revision=revision)
        await state.set_active_schedule_version(schedule.version)

        from schedule_editor import save_local_schedule

        try:
            await save_local_schedule(
                document,
                based_on_version=self.server_version,
                revision=revision,
            )
            self._local_persisted = True
        except Exception as exc:
            # The edit is already driving the relays; a card that cannot be
            # written must not undo it. It simply will not survive a restart.
            self._local_persisted = False
            await state.log(
                f"[schedule] Local edit is in effect but could not be saved: {exc}",
                level=logging.WARNING,
            )

        await state.log(
            f"[schedule] Running a locally edited schedule (revision {revision}, "
            f"on top of published v{self.server_version}) — the next publish replaces it",
            level=logging.WARNING,
        )

        await self.evaluate(state)
        return schedule

    async def restore_local(self, local, state) -> Schedule:
        """
        Re-adopt a saved override at boot, without re-saving or re-numbering it.

        Raises ``ScheduleError`` if the stored document no longer parses; the
        caller discards it and falls back to the published schedule.
        """
        schedule = await asyncio.to_thread(parse_schedule, local.document)
        self.set_schedule(schedule, local.document, local_revision=local.revision)
        await state.set_active_schedule_version(schedule.version)
        return schedule

    async def revert_local(self, state) -> Schedule | None:
        """
        Throw the local edits away and go back to the published programme.

        Returns the schedule now in force, or None if nothing has ever been
        published — in which case the relays are simply left where they are,
        since an empty schedule switches nothing.
        """
        from schedule_editor import clear_local_schedule

        await clear_local_schedule()
        self._local_persisted = True

        document = self._server_document
        if document is None:
            self._schedule = None
            self._document = None
            self._local_revision = 0
            self._applied = {}
            return None

        schedule = await asyncio.to_thread(parse_schedule, document)
        self.set_schedule(schedule, document)
        await state.set_active_schedule_version(schedule.version)
        await self.evaluate(state)
        return schedule

    # -- rendering -----------------------------------------------------------

    def describe_weekly_rules(self) -> list[str]:
        """Numbered weekly rules, in document order — the order edits use."""
        schedule = self._schedule
        if schedule is None or not schedule.weekly_rules:
            return ["Weekly rules: none"]

        reverse_days = {index: name for name, index in WEEKDAYS.items()}
        lines = ["Weekly rules:"]
        for position, rule in enumerate(schedule.weekly_rules, start=1):
            days = ", ".join(reverse_days[d][:3] for d in sorted(rule.days))
            targets = ", ".join(str(t) for t in rule.targets)
            lines.append(
                f"  {position}) {rule.start:%H:%M}-{rule.end:%H:%M} {days} -> "
                f"{'ON' if rule.state else 'OFF'}  [{targets}]"
            )
        return lines

    def describe_exceptions(self) -> list[str]:
        schedule = self._schedule
        if schedule is None or not schedule.exceptions:
            return ["Exceptions: none"]

        lines = ["Exceptions:"]
        for position, exception in enumerate(schedule.exceptions, start=1):
            window = (
                "all day"
                if exception.all_day or exception.start is None
                else f"{exception.start:%H:%M}-"
                f"{(exception.end or datetime.time.max):%H:%M}"
            )
            targets = ", ".join(str(t) for t in exception.targets)
            lines.append(
                f"  {position}) {exception.date} {window} -> "
                f"{'ON' if exception.state else 'OFF'}  [{targets}]"
                f"  ({exception.exception_id}: {exception.reason})"
            )
        return lines

    def describe_now(self) -> list[str]:
        """What the schedule wants for each target at this moment."""
        schedule = self._schedule
        if schedule is None:
            return ["Now: no schedule"]

        lines = ["Now:"]
        for target, want_on in sorted(schedule.desired_states().items()):
            relay_id = relay_for_target(target)
            name = RELAYS.get(relay_id, {}).get("name", "unmapped") if relay_id else "unmapped"
            lines.append(
                f"  {target}: {'ON' if want_on else 'OFF'} "
                f"({name}{f', relay {relay_id}' if relay_id else ''})"
            )
        return lines

    def describe(self) -> list[str]:
        """Human-readable summary for the control menu."""
        schedule = self._schedule
        if schedule is None:
            return ["No schedule received yet."]

        local = schedule.local_time()
        lines = [
            f"Schedule v{schedule.version} "
            f"(tz={schedule.timezone_name or 'system'}, local time {local:%Y-%m-%d %H:%M})"
        ]
        if self.is_locally_modified:
            lines.append(
                f"  LOCALLY EDITED on this device (revision {self._local_revision}, "
                f"on top of published v{self.server_version}) — "
                "the server's next publish replaces it"
            )
            if not self._local_persisted:
                lines.append("  NOT SAVED to disk — these edits will not survive a restart")

        lines.extend(self.describe_weekly_rules())
        lines.extend(self.describe_exceptions())
        lines.extend(self.describe_now())
        return lines


schedule_runner = ScheduleRunner()
