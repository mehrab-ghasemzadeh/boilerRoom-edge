"""
Local schedule editing.

Schedules are published from the cloud, but an operator standing in the boiler
room has to be able to change the heating programme without it — the same
argument that put control modes on the device (menu option 7). A site whose
uplink is dead, or a room being commissioned before anything has been
published, still needs its programme in someone's hands.

Every edit is a mutation of the raw ``schedule.apply`` document and is handed
back to ``parse_schedule`` before it is accepted, so a locally built schedule
passes exactly the checks a server push does. There is deliberately no second,
weaker validator here to drift out of step with the real one.

An edit is **offered upstream**: ``schedule.update`` (DEVICE.md) hands the new
weekly rules to the server, which creates a schedule version for the room and
names it in ``schedule.update_ack``. The edit then stops being local at all —
it *is* the room's schedule, and the server pushes it to the room's other
devices.

Everything below is what happens until that succeeds, which matters because the
device is expected to run through outages:

  * the edit drives the relays immediately, published or not
  * it is held as an override on the last published version, and retried on
    the next connection
  * while unpublished the device keeps reporting the *published*
    ``schedule_version`` — bumping it would only make the server notice a
    mismatch on the next hello and push its own schedule back, wiping the
    change before it could be offered
  * a ``schedule.apply`` supersedes an unpublished edit and deletes the
    override: the room's owner published something, and that outranks a local
    edit that never made it up

An unpublished edit is a divergence the server cannot see, so everything that
can show it does — the control menu, the log at WARNING, and ``device.state``.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from json_store import read_json, write_json
from load_env import env_path
from schedule_runner import WEEKDAYS, Target

# The override lives beside the server's cache rather than overwriting it:
# discarding an edit has to be able to put the published programme back, and a
# device that boots after the server published something newer has to be able
# to tell that its edits are stale.
SCHEDULE_LOCAL_PATH = env_path("BOILERROOM_SCHEDULE_LOCAL", "schedule_local.json")

# Canonical day names, Monday first, spelled the way parse_schedule wants them.
DAY_NAMES = tuple(sorted(WEEKDAYS, key=lambda name: WEEKDAYS[name]))

# Three-letter forms, which is what an operator actually types.
_DAY_ALIASES = {name[:3]: name for name in DAY_NAMES}

# "all" is spelled out; there is deliberately no "weekdays"/"weekends"
# shorthand. The working week is not the same everywhere — this platform's own
# schedules run on Asia/Tehran — and a wrong guess would quietly heat the room
# on the wrong days.
_DAY_GROUPS = {"all": DAY_NAMES, "daily": DAY_NAMES, "everyday": DAY_NAMES}


class ScheduleEditError(ValueError):
    """Raised when an operator's input cannot be turned into a schedule edit."""


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Reading what the operator typed
# ---------------------------------------------------------------------------
#
# These only normalise: whether the result is a *valid* schedule is decided by
# parse_schedule, exactly as it is for a document that arrived from the server.


def parse_days(raw: str) -> list[str]:
    """``"mon,tue"`` or ``"all"`` -> canonical day names."""
    text = raw.strip().lower()
    if not text:
        raise ScheduleEditError("no days given")
    if text in _DAY_GROUPS:
        return list(_DAY_GROUPS[text])

    days: list[str] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        name = _DAY_ALIASES.get(part[:3])
        if name is None or not name.startswith(part):
            raise ScheduleEditError(
                f"unknown day {part!r} — use three letters or the full name "
                f"({', '.join(n[:3] for n in DAY_NAMES)}), or 'all'"
            )
        if name not in days:
            days.append(name)

    if not days:
        raise ScheduleEditError("no days given")
    return days


def parse_time_text(raw: str, field: str) -> str:
    """``"7:30"`` -> ``"07:30"``."""
    text = raw.strip()
    if not text:
        raise ScheduleEditError(f"{field}: a time is required, as HH:MM")
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).strftime("%H:%M")
        except ValueError:
            continue
    raise ScheduleEditError(f"{field}: cannot read {text!r} as a HH:MM time")


def parse_state_text(raw: str) -> bool:
    text = raw.strip().lower()
    if text in ("on", "1", "true", "yes", "y", "heat_on"):
        return True
    if text in ("off", "0", "false", "no", "n", "heat_off"):
        return False
    raise ScheduleEditError(f"cannot read {raw.strip()!r} as on or off")


def parse_date_text(raw: str, *, today: datetime.date) -> str:
    """
    ``"today"``, ``"tomorrow"`` or ``"YYYY-MM-DD"`` -> an ISO date.

    ``today`` is passed in rather than read from the clock here: exceptions are
    matched in the *schedule's* timezone, and a Pi running on UTC under an
    Asia/Tehran schedule can already be on the next day there.
    """
    text = raw.strip().lower()
    if text in ("today", "t"):
        return today.isoformat()
    if text in ("tomorrow", "tom"):
        return (today + datetime.timedelta(days=1)).isoformat()
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ScheduleEditError(
            f"cannot read {raw.strip()!r} as a date — use YYYY-MM-DD, 'today' or 'tomorrow'"
        ) from exc


# ---------------------------------------------------------------------------
# Document edits
# ---------------------------------------------------------------------------
#
# Each returns a new document; none of them mutate the one they are given, so a
# rejected edit leaves the running schedule untouched.


def update_payload(document: dict[str, Any]) -> dict[str, Any]:
    """
    A ``schedule.update`` payload built from an edited document (DEVICE.md).

    ``schedule_version`` is deliberately left out: the server creates the new
    version and tells us which it is in ``schedule.update_ack``.

    ``exceptions`` and ``timezone`` are documented as optional but are always
    sent, because the server builds a whole new schedule from this message.
    Omitting an empty ``exceptions`` would leave it ambiguous whether the
    exceptions an operator has just deleted should survive.
    """
    return {
        "timezone": str(document.get("timezone") or ""),
        "weekly_rules": document.get("weekly_rules") or [],
        "exceptions": document.get("exceptions") or [],
    }


def blank_document(*, version: int = 0, timezone_name: str = "") -> dict[str, Any]:
    """
    An empty programme, for a device that has never been given one.

    It keeps version 0, so the server's first publish is newer and supersedes
    it. An empty timezone means system local time — the schedule the server
    eventually pushes brings the real one with it.
    """
    return {
        "schedule_version": version,
        "timezone": timezone_name,
        "weekly_rules": [],
        "exceptions": [],
    }


def _editable(document: dict[str, Any]) -> dict[str, Any]:
    edited = copy.deepcopy(document)
    if not isinstance(edited.get("weekly_rules"), list):
        edited["weekly_rules"] = []
    if not isinstance(edited.get("exceptions"), list):
        edited["exceptions"] = []
    return edited


def _rule_targets(targets: Sequence[Target]) -> list[dict[str, Any]]:
    if not targets:
        raise ScheduleEditError("a rule needs at least one target")
    return [{"type": target.type, "index": target.index} for target in targets]


def _exception_target(targets: Sequence[Target]) -> dict[str, Any]:
    """Exceptions address units as index lists rather than target objects."""
    if not targets:
        raise ScheduleEditError("an exception needs at least one target")

    payload: dict[str, Any] = {}
    boilers = sorted({t.index for t in targets if t.type == "boiler"})
    pumps = sorted({t.index for t in targets if t.type == "pump"})
    if boilers:
        payload["boiler_indexes"] = boilers
    if pumps:
        payload["pump_indexes"] = pumps
    return payload


def _next_exception_id(document: dict[str, Any]) -> str:
    """
    A recognisable id for an exception added here.

    The ``local-`` prefix is how an operator tells their own one-offs apart
    from the ones that came down with a published schedule.
    """
    existing = {
        str(entry.get("exception_id") or "")
        for entry in document.get("exceptions", [])
        if isinstance(entry, dict)
    }
    number = 1
    while f"local-{number}" in existing:
        number += 1
    return f"local-{number}"


def add_weekly_rule(
    document: dict[str, Any],
    *,
    days: Sequence[str],
    start: str,
    end: str,
    state: bool,
    targets: Sequence[Target],
) -> dict[str, Any]:
    edited = _editable(document)
    edited["weekly_rules"].append({
        "days": list(days),
        "start": start,
        "end": end,
        "state": "on" if state else "off",
        "targets": _rule_targets(targets),
    })
    return edited


def remove_weekly_rule(document: dict[str, Any], position: int) -> dict[str, Any]:
    """Drop rule ``position`` (1-based, in document order)."""
    edited = _editable(document)
    rules = edited["weekly_rules"]
    if not 1 <= position <= len(rules):
        raise ScheduleEditError(
            f"there is no weekly rule {position}"
            + (f" — there are {len(rules)}" if rules else " — there are none")
        )
    rules.pop(position - 1)
    return edited


def add_exception(
    document: dict[str, Any],
    *,
    date: str,
    state: bool,
    targets: Sequence[Target],
    all_day: bool = True,
    start: str | None = None,
    end: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    edited = _editable(document)
    entry: dict[str, Any] = {
        "exception_id": _next_exception_id(edited),
        "date": date,
        "action": "heat_on" if state else "heat_off",
        "all_day": bool(all_day),
        "target": _exception_target(targets),
        "reason": reason or "set on the device",
    }
    if not all_day:
        entry["start"] = start
        entry["end"] = end
    edited["exceptions"].append(entry)
    return edited


def remove_exception(document: dict[str, Any], position: int) -> dict[str, Any]:
    """Drop exception ``position`` (1-based, in document order)."""
    edited = _editable(document)
    exceptions = edited["exceptions"]
    if not 1 <= position <= len(exceptions):
        raise ScheduleEditError(
            f"there is no exception {position}"
            + (f" — there are {len(exceptions)}" if exceptions else " — there are none")
        )
    exceptions.pop(position - 1)
    return edited


# ---------------------------------------------------------------------------
# On-disk override
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalSchedule:
    """A locally edited programme, as it is stored."""

    document: dict[str, Any]
    # The server version these edits were made on top of. If the cached server
    # schedule has moved past it, the edits are stale and get dropped.
    based_on_version: int
    revision: int
    saved_at: str = ""


async def load_local_schedule(
    path: Path | None = None,
) -> tuple[LocalSchedule | None, str | None]:
    """
    Load the local override.

    Returns ``(local, error)``. No file yields ``(None, None)`` — an unedited
    device is the normal case, not a fault. Only the wrapper is checked here;
    the document itself is validated by ``parse_schedule`` when it is adopted,
    so it is parsed once rather than twice on a single-core boot.
    """
    payload = await read_json(path or SCHEDULE_LOCAL_PATH)
    if payload is None:
        return None, None

    document = payload.get("schedule")
    if not isinstance(document, dict):
        return None, "no 'schedule' object"

    based_on = payload.get("based_on_version")
    revision = payload.get("revision")
    if not isinstance(based_on, int) or not isinstance(revision, int):
        return None, "'based_on_version' and 'revision' must be integers"

    return (
        LocalSchedule(
            document=document,
            based_on_version=based_on,
            revision=revision,
            saved_at=str(payload.get("saved_at") or ""),
        ),
        None,
    )


async def save_local_schedule(
    document: dict[str, Any],
    *,
    based_on_version: int,
    revision: int,
    path: Path | None = None,
) -> None:
    """Persist a local override. Written atomically, like the other caches."""
    await write_json(
        path or SCHEDULE_LOCAL_PATH,
        {
            "saved_at": _utc_now_iso(),
            "based_on_version": based_on_version,
            "revision": revision,
            "schedule": document,
        },
    )


async def clear_local_schedule(path: Path | None = None) -> None:
    """Remove the override, leaving the server's cached schedule in charge."""
    target = path or SCHEDULE_LOCAL_PATH
    await asyncio.to_thread(target.unlink, missing_ok=True)
