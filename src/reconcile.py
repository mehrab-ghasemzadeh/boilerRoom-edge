"""
Bringing the device and the server back into agreement after a reconnect.

A device is expected to run through outages, so the two sides drift apart on
purpose: the boiler room keeps working from its caches, an operator can change
the programme and the temperatures at the panel, and meanwhile someone in the
app can change the same things. When the link comes back, somebody has to
decide whose version stands. That used to happen piecemeal — a bit at hello, a
bit whenever the record poll next came round, up to fifteen minutes later — and
some of it not at all.

What "newer" can actually mean
------------------------------
Only two of these things are datable, and it is worth being blunt about which:

* **Schedule and config** carry integer versions, minted by the server, and
  ``hello_ack`` names the ones it wants. Newest-wins is a real comparison here,
  and it is what the reconciler does.
* **Modes and setpoints** carry no version and no timestamp — not in the unit
  record (``desired_mode``, ``desired_temperature_c`` and their ``reported_``
  twins are bare values), not in telemetry, and not on this device. There is
  nothing to compare, so "keep the newer one" cannot be evaluated literally.

For those, the rule is the one thing that *is* known: whether this device has
managed to tell the server yet. A change made here and not yet sent is by
definition newer than anything the server holds, because the server has never
heard of it — so it wins and is published. Anything else, the record wins. If
the platform ever puts an ``updated_at`` on a unit, this is the place to make
it a real comparison.

* **Relay wiring, calibration, enabled probes** are server-owned. The device
  has no way to author them, so the record simply wins.
* **Relay on/off** is the opposite: the device owns it physically, and the
  server learns it from telemetry. So the device asserts it rather than asking.

Order matters
-------------
The steps run in a fixed order, and the reason is the case in the docstring
above: a device that edited its schedule while offline, and a server that
published a newer one meanwhile. Publishing the local edit first would make the
stale edit the room's real schedule and destroy the newer one — the device
would win by being first to speak, not by being right. So versions settle
first, and only what survives that gets offered upstream.

Each step is independent: one failing is logged and the rest still run, because
a reconnect that fixes four things out of five is better than one that gives up
on the first error.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from device_record import DeviceRecordError, fetch_device_record
from record_sync import adopt_device_record
from runtime_state import RuntimeState
from schedule_runner import schedule_runner

# How long to wait for a config/schedule the server said it wants us on.
# DEVICE.md says the server "may push" after hello; there is no message a
# device can send to ask for one, so this is a grace period, not a request.
PUSH_GRACE_SECONDS = 20.0
PUSH_POLL_SECONDS = 0.5


async def reconcile_after_hello(state: RuntimeState, hello_ack: dict[str, Any]) -> None:
    """Bring device and server into agreement, then report where we ended up."""
    await state.log("[sync] Reconciling with the server after hello ...")

    steps = (
        ("versions", _settle_versions),
        ("device record", _adopt_server_record),
        ("local changes", _publish_local_changes),
        ("thermal backlog", _flush_thermal_backlog),
        ("relay state", _assert_relay_state),
    )

    for label, step in steps:
        if state.shutdown.is_set():
            return
        try:
            await step(state, hello_ack)
        except Exception as exc:
            # One step failing must not cost the others. A reconnect that
            # fixes four things out of five beats one that stops at the first.
            await state.log(
                f"[sync] Could not reconcile {label}: {exc}", level=logging.ERROR
            )

    await state.log("[sync] Reconciliation complete")


# ---------------------------------------------------------------------------
# 1. Schedule and config versions
# ---------------------------------------------------------------------------


def _version(raw: Any) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


async def _settle_versions(state: RuntimeState, hello_ack: dict[str, Any]) -> None:
    """
    Compare what the server wants us running against what we are running.

    ``hello_ack`` was previously recorded and never acted on, so a device whose
    schedule was older than the room's sat on the stale one until the server
    happened to push — which DEVICE.md only promises as a "may".

    A device can never legitimately be *ahead*: versions are minted by the
    server, so this device holding a higher number means the room was rolled
    back, or something was lost server-side. That is reported, not fought — the
    server's own push settles it. The genuine "the device has something newer"
    case is a local edit, which keeps the published version number on purpose
    and is handled by the next step.
    """
    payload = (hello_ack or {}).get("payload") or {}
    wanted_config = _version(payload.get("desired_config_version"))
    wanted_schedule = _version(payload.get("desired_schedule_version"))
    active_config, active_schedule = await state.get_active_versions()

    if wanted_config is None and wanted_schedule is None:
        await state.log("[sync] hello_ack named no desired versions — nothing to compare")
        return

    for name, wanted, active in (
        ("config", wanted_config, active_config),
        ("schedule", wanted_schedule, active_schedule),
    ):
        if wanted is not None and active > wanted:
            await state.log(
                f"[sync] This device runs {name} v{active} but the server wants "
                f"v{wanted} — the room was rolled back, or the server lost a "
                "version. Waiting for it to push; nothing is sent up, because a "
                "version number only means something on the server that minted it",
                level=logging.WARNING,
            )

    behind = [
        f"{name} v{active} -> v{wanted}"
        for name, wanted, active in (
            ("config", wanted_config, active_config),
            ("schedule", wanted_schedule, active_schedule),
        )
        if wanted is not None and wanted > active
    ]

    if not behind:
        await state.log(
            f"[sync] Versions agree — config v{active_config}, schedule v{active_schedule}"
        )
        return

    await state.log(
        f"[sync] The server has newer documents ({', '.join(behind)}) — "
        f"waiting up to {PUSH_GRACE_SECONDS:.0f}s for it to push them"
    )

    if await _wait_for_versions(state, wanted_config, wanted_schedule):
        config_version, schedule_version = await state.get_active_versions()
        await state.log(
            f"[sync] Adopted the server's documents — config v{config_version}, "
            f"schedule v{schedule_version}"
        )
        return

    config_version, schedule_version = await state.get_active_versions()
    await state.log(
        f"[sync] The server did not push within {PUSH_GRACE_SECONDS:.0f}s. Still "
        f"running config v{config_version}, schedule v{schedule_version}; the "
        "device keeps working from these and asks again on the next hello — "
        "there is no message a device can send to request one",
        level=logging.WARNING,
    )


async def _wait_for_versions(
    state: RuntimeState,
    wanted_config: int | None,
    wanted_schedule: int | None,
) -> bool:
    """
    Wait for the listen loop to apply what the server pushes.

    Polled rather than event-driven: ``config.apply`` and ``schedule.apply``
    are handled on the listen task, which is already running, and their
    handlers set the active versions as their last act.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PUSH_GRACE_SECONDS

    while True:
        config_version, schedule_version = await state.get_active_versions()
        if (wanted_config is None or config_version >= wanted_config) and (
            wanted_schedule is None or schedule_version >= wanted_schedule
        ):
            return True

        if state.shutdown.is_set() or loop.time() >= deadline:
            return False

        try:
            await asyncio.wait_for(state.shutdown.wait(), timeout=PUSH_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# 2. The device record
# ---------------------------------------------------------------------------


async def _adopt_server_record(state: RuntimeState, _hello_ack: dict[str, Any]) -> None:
    """
    Re-fetch the record and reconcile modes, setpoints, calibration and wiring.

    This is the only carrier for most of what the two sides can disagree about:
    nothing on the WebSocket reports a unit's current mode or setpoint, and
    after hello the server re-dispatches only commands still queued or sent, so
    a mode changed in the app during the outage never arrives as a command.

    Fetched here rather than left to the record loop's fifteen-minute tick,
    which is how long a device could otherwise run on modes the app had already
    changed. Under the same lock as that loop, so the two cannot interleave and
    apply halves of two different records.
    """
    async with state.record_lock:
        try:
            record, payload = await fetch_device_record()
        except DeviceRecordError as exc:
            await state.log(
                f"[sync] Server record unusable: {exc} — keeping the cached one",
                level=logging.WARNING,
            )
            return

        await adopt_device_record(state, record, payload)

    await state.log("[sync] Modes, setpoints and calibration checked against the record")


# ---------------------------------------------------------------------------
# 3. What this device changed while it was on its own
# ---------------------------------------------------------------------------


async def _publish_local_changes(state: RuntimeState, _hello_ack: dict[str, Any]) -> None:
    """
    Offer everything changed here that the server has not heard about.

    Deliberately *after* the version settle and the record fetch. A local
    schedule edit made offline is discarded by a newer published schedule, so
    running this first would have published a stale edit and made it the room's
    real programme — the device winning by speaking first rather than by being
    right.
    """
    from ws_client import (
        _publish_pending_modes,
        _publish_pending_schedule,
        _publish_pending_setpoints,
    )

    if schedule_runner.is_locally_modified:
        await state.log(
            f"[sync] This device holds {schedule_runner.local_revision} schedule "
            f"edit(s) on top of published v{schedule_runner.server_version} that "
            "survived the version check — offering them to the server"
        )

    await _publish_pending_schedule(state)
    await _publish_pending_setpoints(state)
    await _publish_pending_modes(state)


# ---------------------------------------------------------------------------
# 4. Readings taken while the link was down
# ---------------------------------------------------------------------------


async def _flush_thermal_backlog(state: RuntimeState, _hello_ack: dict[str, Any]) -> None:
    """
    Deliver telemetry that could not be posted during the outage.

    Before the relay-state assertion below, because ``post_telemetry`` refuses
    to send a live envelope ahead of a backlog: older envelopes carry
    ``boiler_states``/``pump_states`` too, and delivering them afterwards would
    rewind the server's view of the relays.
    """
    from telemetry_client import drain_outbox

    delivered, remaining = await drain_outbox(state)

    if delivered:
        await state.log(f"[sync] Delivered {delivered} envelope(s) held during the outage")
    if remaining:
        await state.log(
            f"[sync] {remaining} envelope(s) still queued — the regular telemetry "
            "cycle keeps draining them, a batch at a time"
        )
    elif not delivered:
        await state.log("[sync] No thermal backlog to deliver")


# ---------------------------------------------------------------------------
# 5. Relay state
# ---------------------------------------------------------------------------


async def _assert_relay_state(state: RuntimeState, _hello_ack: dict[str, Any]) -> None:
    """
    Tell the server where the relays actually are.

    The one thing not negotiated: the device owns relay positions physically,
    and DEVICE.md has ``reported_state`` come from telemetry and nothing else.
    So this asserts rather than compares — the schedule and the limit guard
    decided these positions, and the cloud's ``desired_state`` does not get to
    overrule a safety cut.

    Routed through the state publisher rather than posting here, so it obeys
    the same debounce and minimum gap as every other change-driven post: on a
    Pi Zero W each one is a TLS handshake on a single ARMv6 core.
    """
    from ws_client import push_device_state

    await push_device_state(state)
    await state.notify_state_change("reconnected to the server")
