"""
Temperature limit enforcement.

``config.apply`` carries operating limits. This module enforces them: a boiler
is cut when its water temperature reaches ``max_water_temperature_c`` and comes
back once the temperature has fallen to ``min_water_temperature_c``. The gap
between the two is a deadband — without it a boiler sitting at the limit would
switch on and off on every read cycle.

A cut outranks everything else:

  * the scheduler will not switch a cut boiler on
  * ``boiler.turn_on`` is refused while cut
  * the control menu refuses to toggle it on

Recovery hands control back to whoever had it: in automatic mode the schedule
re-asserts whatever it wants right now, in manual mode the boiler returns to the
state it was in before the cut.

Two probe topologies exist in the field, and they are judged differently.

**Two or more probes on the unit** — inlet and outlet, say. The gradient across
the boiler is measurable, so ``max_water_temperature_c`` is a *ceiling*: cut on
reaching it, restore at ``min_water_temperature_c``, or one fallback deadband
below the maximum when the server sends no minimum.

**One probe on the unit.** A single probe cannot tell flow from return, so it
reads as the boiler's own temperature. The configured value is a *ceiling*: cut
on reaching ``max_water_temperature_c``, restore ``BOILERROOM_SINGLE_PROBE_BAND``
below it (3 °C). This keeps the boiler from ever exceeding the configured
maximum, while the deadband prevents rapid on/off cycling around the setpoint.

The topology is read from the **mapping**, never from which probes happen to be
readable this cycle. Deciding it from live readings would let a failed outlet
probe quietly move a two-probe boiler onto the single-probe band — changing a
safety threshold because of a fault.

Sensor selection:
  cut     — the hottest of this unit's outlet, inlet and body probes, so a
            runaway on any of them trips
  restore — the *control* probe, in order of preference: outlet, then body,
            then inlet. Outlet is what the boiler is actually delivering and
            body is the next best proxy for it; inlet comes last because
            return water commonly sits above the restore point, and judging
            recovery on it would hold a boiler off indefinitely. A cut
            additionally will not clear while any probe is still at or above
            the cut-out, so a stuck-hot probe cannot cause on/off chatter.
  ambient — environment_inside only; outdoor temperature must not cut boilers

A boiler the mapping gives no probe at all, or that the config gives no maximum
for, has no over-temperature cut. That is said out loud at WARNING rather than
left silent — it is how boilers 1 and 2 of this installation ran unprotected:
their only probe was ``boiler_body``, which the cut did not look at.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from config import RELAYS, TEMPERATURE_SENSORS
from errors_client import build_error, schedule_post_errors
from schedule_runner import Target, relay_for_target, schedule_runner

WATER_ROLES = ("boiler_output_water", "boiler_input_water")
BODY_ROLE = "boiler_body"

# Every probe that measures the boiler itself. A runaway on any of them trips.
CUT_ROLES = WATER_ROLES + (BODY_ROLE,)

# Which probe recovery is judged on, best first. See the module docstring.
CONTROL_ROLE_PRIORITY = ("boiler_output_water", BODY_ROLE, "boiler_input_water")

AMBIENT_ROLE = "environment_inside"

REASON_WATER = "water_over_temperature"
REASON_AMBIENT = "ambient_over_temperature"

# The room limit has no matching minimum in the config document, so recovery
# needs a deadband of its own.
AMBIENT_HYSTERESIS_C = float(os.environ.get("BOILERROOM_AMBIENT_HYSTERESIS", "2.0"))

# Only used when a config sets a maximum water temperature but no minimum, on a
# unit with two or more probes.
FALLBACK_WATER_DEADBAND_C = float(os.environ.get("BOILERROOM_WATER_DEADBAND", "5.0"))

# Half-band for a unit with a single probe: cut this far above the configured
# temperature, restore this far below it.
SINGLE_PROBE_BAND_C = float(os.environ.get("BOILERROOM_SINGLE_PROBE_BAND", "3.0"))


@dataclass
class _CutRecord:
    reason: str
    was_on: bool


@dataclass(frozen=True)
class _NoLimits:
    """Stand-in for describe() when a boiler has a setpoint but no config yet."""

    max_water_temperature_c: float | None = None
    min_water_temperature_c: float | None = None
    max_ambient_temperature_c: float | None = None


_NO_LIMITS = _NoLimits()


def _boiler_targets() -> list[Target]:
    """Every boiler that has a relay in the device mapping."""
    targets: list[Target] = []
    for cfg in RELAYS.values():
        if cfg.get("role") != "pot":
            continue
        unit = cfg.get("unit") or ""
        if not unit.startswith("pot_"):
            continue
        try:
            targets.append(Target("boiler", int(unit.split("_", 1)[1])))
        except ValueError:
            continue
    return sorted(set(targets))


def _readings_for(
    temperatures: dict[int, float | None],
    *,
    roles: tuple[str, ...],
    unit: str | None = None,
) -> list[float]:
    values: list[float] = []
    for sensor_id, value in temperatures.items():
        if value is None:
            continue
        cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
        if cfg.get("role") not in roles:
            continue
        if unit is not None and cfg.get("unit") != unit:
            continue
        values.append(value)
    return values


def _unit_probes(unit: str) -> list[str]:
    """
    Roles of the boiler probes the *mapping* puts on this unit.

    Read from the mapping rather than from a cycle's readings, because it
    decides which threshold applies: a probe that has merely gone unreadable
    must not move the unit onto a different safety band.
    """
    return [
        cfg["role"]
        for cfg in TEMPERATURE_SENSORS.values()
        if cfg.get("unit") == unit and cfg.get("role") in CUT_ROLES
    ]


def _hottest_temperature(unit: str, temperatures: dict[int, float | None]) -> float | None:
    """Hottest probe on this unit — what the over-temperature cut trips on."""
    values = _readings_for(temperatures, roles=CUT_ROLES, unit=unit)
    return max(values) if values else None


def _control_temperature(unit: str, temperatures: dict[int, float | None]) -> float | None:
    """Outlet if present, else body, else inlet — what recovery is judged on."""
    for role in CONTROL_ROLE_PRIORITY:
        values = _readings_for(temperatures, roles=(role,), unit=unit)
        if values:
            return max(values)
    return None


def _ambient_temperature(temperatures: dict[int, float | None]) -> float | None:
    values = _readings_for(temperatures, roles=(AMBIENT_ROLE,))
    return max(values) if values else None


def _restore_threshold(limits) -> float | None:
    if limits.min_water_temperature_c is not None:
        return limits.min_water_temperature_c
    if limits.max_water_temperature_c is not None:
        return limits.max_water_temperature_c - FALLBACK_WATER_DEADBAND_C
    return None


def _thresholds(
    limits,
    *,
    single_probe: bool,
    setpoint: float | None = None,
) -> tuple[float | None, float | None]:
    """
    ``(cut_at, restore_at)`` in °C for one boiler.

    The target is that boiler's own setpoint where it has one (DEVICE.md's
    per-unit ``desired_temperature_c``), and the device-wide
    ``max_water_temperature_c`` where it does not. A per-unit setpoint is the
    more specific instruction, so it wins.

    ``(None, None)`` when there is neither — the boiler then has no
    over-temperature cut, which the caller reports.
    """
    target = setpoint if setpoint is not None else limits.max_water_temperature_c
    if target is None:
        return None, None

    if single_probe:
        # One probe reads as the boiler's own temperature, so the configured
        # maximum is a hard ceiling: cut on reaching it, restore one band below
        # to avoid rapid cycling.
        return target, target - SINGLE_PROBE_BAND_C

    if setpoint is not None:
        # A per-unit setpoint has no matching per-unit minimum, so recovery
        # needs the fallback deadband; the device-wide min would be answering
        # a different question.
        return target, target - FALLBACK_WATER_DEADBAND_C

    return target, _restore_threshold(limits)


class LimitGuard:
    """Cuts and restores boilers according to the pushed config limits."""

    def __init__(self) -> None:
        self._cut: dict[Target, _CutRecord] = {}
        # Boilers currently known to have no over-temperature cut, so the
        # warning is raised on entering that state rather than every cycle.
        self._unprotected: set[Target] = set()

    @property
    def cut_targets(self) -> dict[Target, str]:
        return {target: record.reason for target, record in self._cut.items()}

    async def check(
        self,
        state,
        temperatures: dict[int, float | None],
    ) -> None:
        """Evaluate limits against the latest readings and act on them."""
        config = await state.get_device_config()
        relay_controller = state.relay_controller
        if config is None or relay_controller is None:
            return

        limits = config.limits
        max_ambient = limits.max_ambient_temperature_c

        setpoints = await state.get_setpoints()

        ambient = _ambient_temperature(temperatures)
        ambient_over = (
            max_ambient is not None and ambient is not None and ambient >= max_ambient
        )
        # A room that is still hot, or whose sensor is unreadable, blocks recovery.
        ambient_recovered = max_ambient is None or (
            ambient is not None and ambient < max_ambient - AMBIENT_HYSTERESIS_C
        )

        schedule_dirty = False

        for target in _boiler_targets():
            relay_id = relay_for_target(target)
            if relay_id is None:
                continue

            unit = f"pot_{target.index}"
            probes = _unit_probes(unit)
            single_probe = len(probes) == 1
            entry = setpoints.get(target.index)
            cut_at, restore_at = _thresholds(
                limits,
                single_probe=single_probe,
                setpoint=entry.temperature_c if entry is not None else None,
            )

            await self._report_protection(state, target, probes, cut_at)

            hottest = _hottest_temperature(unit, temperatures)
            control = _control_temperature(unit, temperatures)
            record = self._cut.get(target)

            water_over = (
                cut_at is not None and hottest is not None and hottest >= cut_at
            )

            if record is None:
                await self._maybe_cut(
                    state,
                    relay_controller,
                    target,
                    relay_id,
                    water=hottest,
                    water_over=water_over,
                    cut_at=cut_at,
                    single_probe=single_probe,
                    ambient=ambient,
                    max_ambient=max_ambient,
                    ambient_over=ambient_over,
                )
                continue

            # Already cut. Recovery needs a reading — an unavailable sensor must
            # never be mistaken for a cool boiler. Each cause clears on its own
            # terms, but never while the other cause is still active.
            water_recovered = (
                restore_at is not None
                and control is not None
                and control <= restore_at
                and not water_over
            )

            if record.reason == REASON_WATER:
                can_restore = water_recovered and ambient_recovered
                detail = (
                    f"boiler {control:.1f} °C <= {restore_at:.1f} °C"
                    if control is not None and restore_at is not None
                    else "temperature back in range"
                )
            else:
                can_restore = ambient_recovered and not water_over
                detail = (
                    f"ambient {ambient:.1f} °C back under {max_ambient:.1f} °C"
                    if ambient is not None and max_ambient is not None
                    else "ambient back in range"
                )

            if can_restore:
                await self._restore(
                    state, relay_controller, target, relay_id, record, detail=detail
                )
                schedule_dirty = True

        if schedule_dirty:
            # Let the schedule re-assert anything that just came back.
            await schedule_runner.evaluate(state)

    async def _report_protection(
        self,
        state,
        target: Target,
        probes: list[str],
        cut_at: float | None,
    ) -> None:
        """
        Say whether this boiler has an over-temperature cut at all.

        Reported on the transition rather than every cycle, and retracted when
        it is fixed. Without this a boiler with no usable probe simply never
        trips, which is indistinguishable from one that is never too hot.
        """
        if not probes:
            problem = "the mapping puts no temperature probe on it"
        elif cut_at is None:
            problem = "the config sets no max_water_temperature_c"
        else:
            if target in self._unprotected:
                self._unprotected.discard(target)
                await state.log(f"[limits] {target} is protected again (cut at {cut_at:.1f} °C)")
            return

        if target in self._unprotected:
            return
        self._unprotected.add(target)
        await state.log(
            f"[limits] {target} has NO over-temperature cut — {problem}",
            level=logging.WARNING,
        )

    async def _maybe_cut(
        self,
        state,
        relay_controller,
        target: Target,
        relay_id: int,
        *,
        water: float | None,
        water_over: bool,
        cut_at: float | None,
        single_probe: bool,
        ambient: float | None,
        max_ambient: float | None,
        ambient_over: bool,
    ) -> None:
        reason: str | None = None
        detail = ""

        if water_over and water is not None and cut_at is not None:
            reason = REASON_WATER
            detail = f"boiler {water:.1f} °C >= cut-out {cut_at:.1f} °C" + (
                f" (single probe, restore -{SINGLE_PROBE_BAND_C:.0f} °C)"
                if single_probe
                else ""
            )
        elif ambient_over:
            reason = REASON_AMBIENT
            detail = f"ambient {ambient:.1f} °C >= max {max_ambient:.1f} °C"

        if reason is None:
            return

        was_on = relay_controller.get_state(relay_id)
        await relay_controller.turn_off(relay_id)
        self._cut[target] = _CutRecord(reason=reason, was_on=was_on)
        await state.set_limit_block(target, reason)

        name = RELAYS.get(relay_id, {}).get("name", f"Relay {relay_id}")
        await state.log(
            f"[limits] CUT {target} — {name} (relay {relay_id}) off: {detail}",
            level=logging.WARNING,
        )
        # A safety cut is the last thing that should reach the cloud late.
        await state.notify_state_change(f"relay {relay_id} off (limit: {reason})")

        schedule_post_errors(
            build_error(
                code=reason,
                message=f"{target} cut off: {detail}",
                severity="critical",
                device_state="fault",
                boiler_index=target.index,
            ),
            log=state.log,
        )

    async def _restore(
        self,
        state,
        relay_controller,
        target: Target,
        relay_id: int,
        record: _CutRecord,
        *,
        detail: str,
    ) -> None:
        del self._cut[target]
        await state.clear_limit_block(target)

        # In manual mode the schedule will not touch this target, so put it back
        # the way the operator left it. In automatic mode let the schedule decide.
        if await state.get_mode(target) == "manual":
            if record.was_on:
                await relay_controller.turn_on(relay_id)
            else:
                await relay_controller.turn_off(relay_id)
            await state.notify_state_change(
                f"relay {relay_id} {'on' if record.was_on else 'off'} (limit cleared)"
            )
        else:
            # The schedule re-asserts this target on its next tick and reports
            # the change itself.
            schedule_runner.forget(target)

        await state.log(f"[limits] RESTORED {target} — {detail}")

        schedule_post_errors(
            build_error(
                code=f"{record.reason}_cleared",
                message=f"{target} restored: {detail}",
                severity="info",
                device_state="ok",
                boiler_index=target.index,
            ),
            log=state.log,
        )

    def describe(self, config=None, setpoints: dict[int, Any] | None = None) -> list[str]:
        """
        Per-boiler summary for the control menu.

        Shows the thresholds actually in force, since they depend on that
        boiler's setpoint and on how many probes it has — "cut at 68" and
        "cut at 83" sitting side by side is the kind of thing an operator
        should be able to see rather than deduce from the docs.
        """
        targets = _boiler_targets()
        if not targets:
            return ["Limits: no boilers in the device mapping."]

        limits = getattr(config, "limits", None)
        setpoints = setpoints or {}
        lines = ["Limits:"]

        for target in targets:
            probes = _unit_probes(f"pot_{target.index}")
            single_probe = len(probes) == 1
            entry = setpoints.get(target.index)
            setpoint = entry.temperature_c if entry is not None else None

            source = (
                f"setpoint {setpoint:.1f} °C" if setpoint is not None else "device limit"
            )

            if not probes:
                band = "NO PROBE — not protected"
            elif limits is None and setpoint is None:
                band = "no config yet"
            else:
                cut_at, restore_at = _thresholds(
                    limits or _NO_LIMITS,
                    single_probe=single_probe,
                    setpoint=setpoint,
                )
                if cut_at is None:
                    band = "no temperature to hold — not protected"
                else:
                    band = f"cut {cut_at:.1f} °C, restore " + (
                        f"{restore_at:.1f} °C" if restore_at is not None else "—"
                    )
                    if single_probe:
                        band += f"  [single probe, restore -{SINGLE_PROBE_BAND_C:.0f} °C]"

            record = self._cut.get(target)
            status = (
                f"CUT: {record.reason} (was {'on' if record.was_on else 'off'})"
                if record
                else "ok"
            )
            lines.append(
                f"  {target}: {status}; {source}; "
                f"probes {', '.join(sorted(probes)) or 'none'}; {band}"
            )

        return lines


limit_guard = LimitGuard()
