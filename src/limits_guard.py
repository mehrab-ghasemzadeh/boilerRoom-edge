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

Sensor selection:
  cut     — the hottest of this unit's boiler_output_water and
            boiler_input_water probes, so a runaway on either one trips
  restore — the *control* probe: outlet water if the unit has one, otherwise
            inlet. Restoring on the hottest probe instead would let a warm
            return line hold a boiler off indefinitely, since return water
            commonly sits above min_water_temperature_c. A cut additionally
            will not clear while any probe is still at or above the maximum,
            so a stuck-hot probe cannot cause on/off chatter.
  ambient — environment_inside only; outdoor temperature must not cut boilers
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from config import RELAYS, TEMPERATURE_SENSORS
from errors_client import build_error, schedule_post_errors
from schedule_runner import Target, relay_for_target, schedule_runner

WATER_ROLES = ("boiler_output_water", "boiler_input_water")
AMBIENT_ROLE = "environment_inside"

REASON_WATER = "water_over_temperature"
REASON_AMBIENT = "ambient_over_temperature"

# The room limit has no matching minimum in the config document, so recovery
# needs a deadband of its own.
AMBIENT_HYSTERESIS_C = float(os.environ.get("BOILERROOM_AMBIENT_HYSTERESIS", "2.0"))

# Only used when a config sets a maximum water temperature but no minimum.
FALLBACK_WATER_DEADBAND_C = float(os.environ.get("BOILERROOM_WATER_DEADBAND", "5.0"))


@dataclass
class _CutRecord:
    reason: str
    was_on: bool


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


def _hottest_water(unit: str, temperatures: dict[int, float | None]) -> float | None:
    """Hottest water probe on this unit — what the over-temperature cut uses."""
    values = _readings_for(temperatures, roles=WATER_ROLES, unit=unit)
    return max(values) if values else None


def _control_water(unit: str, temperatures: dict[int, float | None]) -> float | None:
    """Outlet water if present, else inlet — what recovery is judged on."""
    for role in ("boiler_output_water", "boiler_input_water"):
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


class LimitGuard:
    """Cuts and restores boilers according to the pushed config limits."""

    def __init__(self) -> None:
        self._cut: dict[Target, _CutRecord] = {}

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
        max_water = limits.max_water_temperature_c
        max_ambient = limits.max_ambient_temperature_c
        restore_at = _restore_threshold(limits)

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
            hottest = _hottest_water(unit, temperatures)
            control = _control_water(unit, temperatures)
            record = self._cut.get(target)

            water_over = (
                max_water is not None and hottest is not None and hottest >= max_water
            )

            if record is None:
                await self._maybe_cut(
                    state,
                    relay_controller,
                    target,
                    relay_id,
                    water=hottest,
                    water_over=water_over,
                    max_water=max_water,
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
                    f"water {control:.1f} °C <= {restore_at:.1f} °C"
                    if control is not None and restore_at is not None
                    else "water back in range"
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

    async def _maybe_cut(
        self,
        state,
        relay_controller,
        target: Target,
        relay_id: int,
        *,
        water: float | None,
        water_over: bool,
        max_water: float | None,
        ambient: float | None,
        max_ambient: float | None,
        ambient_over: bool,
    ) -> None:
        reason: str | None = None
        detail = ""

        if water_over and water is not None and max_water is not None:
            reason = REASON_WATER
            detail = f"water {water:.1f} °C >= max {max_water:.1f} °C"
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
        else:
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

    def describe(self) -> list[str]:
        if not self._cut:
            return ["Limits: no boiler is currently cut."]
        return ["Limits: cut boilers —"] + [
            f"  {target}: {record.reason} (was {'on' if record.was_on else 'off'})"
            for target, record in sorted(self._cut.items())
        ]


limit_guard = LimitGuard()
