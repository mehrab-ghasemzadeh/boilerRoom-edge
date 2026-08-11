"""
Report local state changes to the server promptly.

The server learns a unit's ``reported_state`` from telemetry
(``boiler_states`` / ``pump_states``) and from nothing else, so a relay or a
control mode changed here — by the operator, by the schedule, or by a
temperature cut — used to sit unreported until the next scheduled post. At a one-minute cadence that is up to
a minute and a bit of the cloud showing the wrong thing, while a change made
*from* the cloud appears instantly because it arrives as a WebSocket command.

This task closes that asymmetry: it watches for local changes and posts
telemetry straight away, plus a ``device.state`` push for any live dashboard.

Two limits keep it cheap on a Pi Zero W, where every POST costs a TLS
handshake on a single ARMv6 core:

* a short debounce, so one schedule tick switching four relays sends one post
* a floor between change-driven posts, so a burst of toggles cannot turn into
  a stream of handshakes
"""

from __future__ import annotations

import asyncio
import logging
import time

from runtime_state import RuntimeState
from telemetry_client import post_telemetry

# Wait this long after the first change so a burst becomes a single post.
DEBOUNCE_SECONDS = 1.0

# Minimum gap between change-driven posts. The regular cadence is unaffected.
MIN_INTERVAL_SECONDS = 5.0


async def _wait_for_change(state: RuntimeState) -> bool:
    """Block until something reportable changes. False if shutdown came first."""
    waiters = [
        asyncio.create_task(state.state_changed.wait()),
        asyncio.create_task(state.shutdown.wait()),
    ]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waiters:
            if not task.done():
                task.cancel()
    return state.state_changed.is_set() and not state.shutdown.is_set()


async def _sleep_unless_shutdown(state: RuntimeState, seconds: float) -> None:
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(state.shutdown.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run_state_publisher(state: RuntimeState) -> None:
    last_post_at: float | None = None

    while not state.shutdown.is_set():
        if not await _wait_for_change(state):
            return

        # Collect anything else that lands in the same moment.
        await _sleep_unless_shutdown(state, DEBOUNCE_SECONDS)
        if state.shutdown.is_set():
            return

        # Cleared after the debounce, not before: a change that arrived during
        # it is already covered by the snapshot about to be sent.
        state.state_changed.clear()
        reasons = await state.take_state_change_reasons()

        # Free and instant where a dashboard is watching; not a substitute for
        # telemetry, which is what actually updates reported_state.
        from ws_client import push_device_state

        pushed = await push_device_state(state)

        if not state.authenticated.is_set():
            # Hold the change rather than dropping it. At boot the schedule
            # switches relays a second or two before login finishes, and
            # discarding that would leave the server wrong until the next
            # scheduled post — a whole read cycle away.
            await state.log(
                "[state] Change pending — waiting for a device session"
            )
            if not await state.wait_authenticated():
                return

        if last_post_at is not None:
            await _sleep_unless_shutdown(
                state, MIN_INTERVAL_SECONDS - (time.monotonic() - last_post_at)
            )
            if state.shutdown.is_set():
                return

        snapshot = await state.get_snapshot()
        try:
            await post_telemetry(state, snapshot["temperatures"], snapshot["gas"])
        except Exception as exc:
            # Leave the regular cadence to retry: reporting is a fraction of a
            # second late either way, and a failed push must not spin.
            await state.log(
                f"[state] Could not report change: {exc}",
                level=logging.WARNING,
            )
            continue

        last_post_at = time.monotonic()
        await state.mark_telemetry_posted()
        await state.log(
            f"[state] Reported {len(reasons)} change(s) immediately"
            + (" (device.state pushed)" if pushed else "")
            + (f": {'; '.join(reasons)}" if reasons else "")
        )
