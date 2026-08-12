"""
Adopting the server's record of this device.

``GET /devices/<id>`` is the only place several things exist: each unit's
``desired_mode`` and ``desired_temperature_c``, calibration offsets, which
probes are disabled, and — since the platform became the source of truth for
the installation — the wiring itself. Nothing pushes any of it, so it is
polled, and it is what a device compares itself against when it has been out of
touch.

Lifted out of ``main`` so the WebSocket can reconcile against the record the
moment a session comes back, rather than waiting out the poll interval. It
cannot import ``main`` to get at it: the agent is started as ``python
src/main.py``, so that module is ``__main__``, and importing it by name would
execute the whole file a second time as a separate module object.
"""

from __future__ import annotations

import logging

from config import load_device_mapping
from device_record import (
    capability_mismatches,
    device_record_store,
    divergences,
    save_cached_device_record,
)
from mapping_provider import MappingUnavailableError, RecordMappingProvider
from mapping_store import mapping_store
from record_mapping import mapping_differences
from runtime_state import RuntimeState


async def load_mapping(state: RuntimeState) -> bool:
    """
    Build the device mapping, tolerating not having one yet.

    The wiring lives in the server record, so an unpaired device genuinely does
    not know what it is connected to. Failing to start would give a restart
    loop under systemd and no way to recover; instead the agent comes up, and
    the record loop sets the mapping once the server supplies it.
    """
    try:
        await load_device_mapping()
    except MappingUnavailableError as exc:
        await state.log(
            f"[mapping] No usable device mapping — {exc}",
            level=logging.WARNING,
        )
        await state.log(
            "[mapping] Waiting for the server record; sensors and relays stay idle "
            "until the installation is described",
            level=logging.WARNING,
        )
        return False
    except Exception as exc:
        await state.log(f"[mapping] Failed to load: {exc}", level=logging.ERROR)
        return False

    state.mapping_ready.set()
    return True


async def adopt_device_record(state: RuntimeState, record, payload) -> None:
    """Make a freshly fetched record active, cache it, and report what it says."""
    # Deferred: ws_client reaches this module through the reconciler, and
    # importing it here at module level would close the loop.
    from ws_client import CAPABILITIES

    previous = device_record_store.record
    device_record_store.set_record(record)
    await save_cached_device_record(payload)

    if previous is None or previous.fetched_at == "":
        await state.log(
            f"[device] Record for {record.public_id}: "
            f"{len(record.sensors)} server sensor(s), "
            f"{len(device_record_store.offsets)} calibration offset(s), "
            f"{len(device_record_store.disabled)} disabled, "
            f"{len(record.boiler_units)} boiler(s), {len(record.pump_units)} pump(s)"
        )

    for uid, offset in sorted(device_record_store.offsets.items()):
        await state.log(f"[device] Calibration {uid}: {offset:+.2f} °C")
    for uid in sorted(device_record_store.disabled):
        await state.log(
            f"[device] Sensor {uid} is disabled server-side — "
            "excluded from telemetry, still used for safety limits",
            level=logging.WARNING,
        )

    # Intent divergence is reported, never applied: the server re-dispatches
    # queued commands after every hello, and acting here could flip a boiler to
    # manual — off the schedule — without a command ever being issued.
    for line in divergences(record):
        await state.log(f"[device] Divergence — {line}", level=logging.WARNING)

    for line in capability_mismatches(record, CAPABILITIES):
        await state.log(f"[device] Capability mismatch — {line}", level=logging.WARNING)

    await adopt_record_modes(state, record)
    await adopt_record_setpoints(state, record)

    # A device that booted without a mapping can start working the moment the
    # server describes it; only then is drift meaningful.
    if not await adopt_mapping_if_missing(state):
        await report_mapping_drift(state, record)


async def adopt_record_modes(state: RuntimeState, record) -> None:
    """
    Take each unit's control mode from the server record.

    This is the only way a mode set in the app reaches the device. Nothing on
    the WebSocket carries current modes: ``hello_ack`` has versions and a
    heartbeat interval, and after hello the server re-dispatches only commands
    still ``queued``/``sent`` — so a mode whose command completed long ago
    never arrives again. ``GET /devices/<id>`` is where it lives.

    This used to be reported and not applied, on the grounds that acting on
    ``desired_mode`` could take a boiler off the schedule without a command
    being issued. That was the wrong call: it left a device booting with every
    unit on ``automatic`` while the app showed ``manual``, which is precisely
    the divergence the reporting was meant to warn about.

    Precedence, when the record and this device disagree:

      * a local change **not yet sent** wins — the server cannot know about it,
        and it is still queued for the next connection
      * otherwise the record wins

    Note "not yet sent", not "not yet acknowledged". The server does not
    implement the mode report yet, so nothing is ever acknowledged; blocking on
    that left every unit permanently claiming to be newer than the record and
    stopped the record correcting anything — the whole reason this function
    exists. Sending is the device's whole obligation.

    ``desired_state`` is still *not* applied. The schedule and the limit guard
    own relay state, and adopting a stale desired on/off would fight them.
    """
    from mode_store import VALID_MODES
    from schedule_runner import Target, schedule_runner

    unpublished = await state.unpublished_modes()

    for unit in record.units:
        desired = (unit.desired_mode or "").strip().lower()
        if desired not in VALID_MODES:
            continue

        target = Target(unit.kind, unit.index)
        current = await state.get_mode(target)

        if current == desired:
            # Agreement: whatever we were trying to report has landed.
            await state.mark_mode_published(target)
            continue

        if target in unpublished:
            await state.log(
                f"[mode] Keeping {target} on {current}: the server shows {desired}, "
                "but this device's change has not reached it yet",
                level=logging.WARNING,
            )
            continue

        await state.set_mode(target, desired, published=True)

        if desired == "automatic":
            # Hand the unit back to the schedule now rather than at its next
            # start/end boundary, as every other mode path does.
            schedule_runner.forget(target)
            await schedule_runner.evaluate(state)

        await state.log(f"[mode] {target} set to {desired} from the server record")
        await state.notify_state_change(f"{target} mode {desired} (server)")


async def adopt_record_setpoints(state: RuntimeState, record) -> None:
    """
    Take each boiler's setpoint from the server record.

    Like ``desired_mode`` above, and for the same reason: DEVICE.md has the
    server push setpoints to devices as ``boiler.apply_temperature``, so the
    record is the same instruction arriving by poll instead of by socket.
    Applying it is what lets a device pick up a temperature set from an app
    while it was offline.

    A setpoint this device has not managed to publish yet outranks the record,
    exactly as an unsent mode does — otherwise a temperature set during an
    outage would be overwritten by the server's older one the moment the link
    came back, which is the failure this whole path exists to prevent.

    Only a change is written, so this costs the card nothing on a steady state.
    """
    from setpoint_store import SetpointError

    unpublished = await state.unpublished_setpoints()

    for unit in record.boiler_units:
        wanted = unit.desired_temperature_c
        if wanted is None:
            continue
        if await state.get_setpoint(unit.index) == wanted:
            continue

        if unit.index in unpublished:
            await state.log(
                f"[setpoint] Keeping boiler {unit.index} at "
                f"{unpublished[unit.index]:.1f} °C: the server shows "
                f"{wanted:.1f} °C, but this device's change has not reached it yet",
                level=logging.WARNING,
            )
            continue

        try:
            await state.set_setpoint(unit.index, wanted, published=True)
        except SetpointError as exc:
            await state.log(
                f"[device] Ignoring boiler {unit.index} setpoint from the record: {exc}",
                level=logging.WARNING,
            )
            continue
        await state.log(
            f"[device] Boiler {unit.index} setpoint {wanted:.1f} °C from the server record"
        )


async def adopt_mapping_if_missing(state: RuntimeState) -> bool:
    """
    Build the mapping from a record that has just arrived.

    Only ever fills a gap: once a mapping is driving the hardware it is left
    alone, because the relay controller configured its pins from it at startup.
    """
    if state.mapping_ready.is_set():
        return False

    if not await load_mapping(state):
        return False

    await state.log("[mapping] Device mapping adopted from the server record")
    return True


async def report_mapping_drift(state: RuntimeState, record) -> None:
    """
    Say so when the record's wiring no longer matches what is running.

    The mapping is deliberately *not* swapped underneath a running agent: the
    relay controller configures its GPIO pins once at startup, so adopting a
    new pin map without re-initialising the hardware would drive pins that were
    never set up. A restart applies it.
    """
    from record_mapping import RecordMappingError, mapping_from_record, readiness_problems

    if not isinstance(mapping_store.provider, RecordMappingProvider):
        return

    problems = readiness_problems(record)
    if problems:
        for problem in problems:
            await state.log(
                f"[mapping] Server record not usable as a mapping — {problem}",
                level=logging.WARNING,
            )
        return

    try:
        document = mapping_from_record(record)
    except RecordMappingError as exc:
        await state.log(f"[mapping] Server record cannot be mapped: {exc}", level=logging.WARNING)
        return

    for line in mapping_differences(document):
        await state.log(f"[mapping] Drift — {line}", level=logging.WARNING)
