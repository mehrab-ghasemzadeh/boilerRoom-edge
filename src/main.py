from load_env import load_dotenv

load_dotenv()

import asyncio
import logging
import signal

from auth import AuthError, MissingCredentialsError, token_manager
from control_menu import run_control_menu
from data_logger import reading_store, save_readings
from device_config import ConfigError, config_store, load_cached_config
from device_record import (
    DeviceRecordError,
    apply_calibration,
    device_record_store,
    fetch_device_record,
    load_cached_device_record,
)
from errors_client import error_reporter
from limits_guard import limit_guard
from logging_setup import configure_logging, shutdown_logging
from mode_store import load_modes
from record_sync import adopt_device_record, load_mapping
from runtime_state import RuntimeState
from schedule_runner import ScheduleError, load_cached_schedule, schedule_runner
from state_publisher import run_state_publisher
from telemetry_client import post_telemetry
from ws_client import run_websocket_client

# ----------------------------------------------------
# Configuration
# ----------------------------------------------------

USE_MOCK_HARDWARE = True

# Sensors are read once a minute, matching the telemetry cadence: at the old
# 10 s there were five discarded cycles for every one that was uploaded, all of
# them costing SD-card writes and CPU on a single-core Pi.
#
# This also sets how quickly the over-temperature cut can react — see the
# limit-latency note in the README.
DEFAULT_READ_INTERVAL = 60

# How often to re-check the active schedule for a state transition
SCHEDULE_TICK_SECONDS = 20

# How often to re-fetch the device's own record. Calibration and enable flags
# change when an installer edits them, which is rare, and nothing pushes the
# change — so poll, but gently.
DEVICE_RECORD_REFRESH_SECONDS = 900

# Login retry backoff, used when the server is unreachable at boot
AUTH_RETRY_DELAY_SECONDS = 10
AUTH_RETRY_MAX_DELAY_SECONDS = 300

# ----------------------------------------------------
# Hardware selection
# ----------------------------------------------------

if USE_MOCK_HARDWARE:
    from mock_temperature_reader import MockTemperatureReader as TemperatureReader
    from mock_gas_reader import MockGasReader as GasReader
    from mock_relay_controller import MockRelayController as RelayController
    # On the bench the control menu is answered from the keyboard, as it always
    # has been. BOILERROOM_KEYPAD_EMULATE=on restricts it to the keys the real
    # keypad has, to find prompts the device could not answer.
    from mock_keypad import MockKeypad as Keypad
else:
    from temperature_reader import TemperatureReader
    from gas_reader import GasReader
    from relay_controller import RelayController
    # The installed device has no keyboard: the menu is answered on the matrix
    # keypad wired to the header.
    from keypad import Keypad


async def print_startup_banner(state: RuntimeState) -> None:
    from config import GAS_SENSORS, RELAYS, TEMPERATURE_SENSORS, UNITS

    await state.echo("========================================")
    await state.echo(" Boiler Room Monitoring System Started")
    await state.echo("========================================\n")

    if not state.mapping_ready.is_set():
        await state.echo(
            "No device mapping yet — this device's wiring comes from the server\n"
            "record. Sensors and relays stay idle until it arrives.\n"
        )
        return

    await state.echo("Equipment units:")
    for unit_id, unit in UNITS.items():
        await state.echo(f"  {unit_id}: {unit['name']}")
    await state.echo("")

    await state.echo("Temperature sensor mapping:")
    for sid, cfg in sorted(TEMPERATURE_SENSORS.items()):
        unit = cfg.get("unit") or "—"
        await state.echo(
            f"  Sensor {sid}: {cfg['name']}  (role={cfg['role']}, unit={unit})"
        )
    await state.echo("")

    await state.echo("Relay mapping:")
    for rid, cfg in sorted(RELAYS.items()):
        unit = cfg.get("unit") or "—"
        await state.echo(
            f"  Relay {rid}: {cfg['name']}  "
            f"(role={cfg['role']}, unit={unit}, GPIO {cfg['gpio']})"
        )
    await state.echo("")


async def sensor_loop(state: RuntimeState) -> None:
    # Hardware is addressed by the mapping — GPIO pins, 1-Wire ROM codes, ADC
    # channels — so there is nothing to construct until one exists.
    if not await state.wait_mapping_ready():
        return

    temperature_reader = TemperatureReader()
    gas_reader = GasReader()
    relay_controller = RelayController()

    state.temperature_reader = temperature_reader
    state.gas_reader = gas_reader
    state.relay_controller = relay_controller

    await asyncio.gather(
        temperature_reader.start(),
        gas_reader.start(),
        relay_controller.start(),
    )

    # Relays exist now, so a cached schedule can take effect immediately rather
    # than waiting for the next schedule tick.
    await schedule_runner.evaluate(state)

    offline_notice_shown = False

    try:
        while not state.shutdown.is_set():
            temperatures, gas = await asyncio.gather(
                temperature_reader.read_all(),
                gas_reader.read_all(),
            )

            # Correct readings before anything consumes them, so the database,
            # the limit guard and telemetry all agree on the value.
            temperatures = apply_calibration(temperatures)

            await state.update_readings(temperatures, gas)
            await save_readings(temperatures, gas)

            await error_reporter.check_temperature_faults(
                temperatures,
                log=state.log,
            )

            # Enforce config limits before telemetry, so a cut is reported in
            # the same cycle it happens.
            await limit_guard.check(state, temperatures)

            # Pacing lives in RuntimeState because a local relay change also
            # posts telemetry; both have to share one "last posted" clock or
            # they double up. The first cycle posts immediately so a fresh boot
            # shows up on the server without waiting out the interval.
            due = await state.telemetry_due()

            if due and not state.authenticated.is_set():
                if not offline_notice_shown:
                    offline_notice_shown = True
                    await state.log("[telemetry] Offline — holding until a session exists")
            elif due:
                offline_notice_shown = False
                await state.mark_telemetry_posted()
                try:
                    await post_telemetry(state, temperatures, gas)
                except Exception as exc:
                    await state.log(f"[telemetry] Failed to post: {exc}", level=logging.WARNING)

            interval = await state.get_read_interval()
            try:
                await asyncio.wait_for(state.shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    finally:
        await asyncio.gather(
            relay_controller.cleanup(),
            gas_reader.close(),
            temperature_reader.close(),
            reading_store.close(),
            return_exceptions=True,
        )


async def schedule_loop(state: RuntimeState) -> None:
    """Re-evaluate the active schedule so relays follow its time boundaries."""
    while not state.shutdown.is_set():
        try:
            await schedule_runner.evaluate(state)
        except Exception as exc:
            await state.log(f"[schedule] Evaluation failed: {exc}", level=logging.ERROR)

        try:
            await asyncio.wait_for(
                state.shutdown.wait(),
                timeout=SCHEDULE_TICK_SECONDS,
            )
        except asyncio.TimeoutError:
            pass


async def auth_loop(state: RuntimeState) -> None:
    """
    Log in, retrying in the background.

    Startup must not depend on the server being reachable: a boiler room that
    reboots during an outage still has to run its heating programme from the
    cached schedule. Everything that needs a session waits on
    ``state.authenticated`` instead of blocking the boot.
    """
    delay = AUTH_RETRY_DELAY_SECONDS

    while not state.shutdown.is_set():
        try:
            session = await token_manager.login()
            state.authenticated.set()
            await state.log(
                f"[auth] Device session established "
                f"(device_id={session.device_id or 'unknown'})"
            )
            return
        except MissingCredentialsError as exc:
            await state.log(f"[auth] {exc}")
            await state.log("[auth] Running offline — telemetry and commands disabled")
            return
        except AuthError as exc:
            await state.log(f"[auth] Login failed: {exc}", level=logging.WARNING)
            await state.log(
                f"[auth] Retrying in {delay:.0f}s — "
                "running from cached schedule until then"
            )

        try:
            await asyncio.wait_for(state.shutdown.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

        delay = min(delay * 2, AUTH_RETRY_MAX_DELAY_SECONDS)


async def restore_cached_schedule(state: RuntimeState) -> None:
    """
    Reload the last schedule the server pushed.

    This is what keeps the boilers on programme when the device boots with no
    network. Reporting the cached version in device.hello also stops the server
    re-pushing an unchanged schedule on every boot.
    """
    schedule, document, error = await load_cached_schedule()

    if error:
        await state.log(
            f"[schedule] Ignoring unusable cache ({error}) — waiting for server push",
            level=logging.WARNING,
        )
    elif schedule is None or document is None:
        await state.log("[schedule] No cached schedule — waiting for server push")
    else:
        schedule_runner.set_server_schedule(schedule, document)
        await state.set_active_schedule_version(schedule.version)
        await state.log(
            f"[schedule] Restored cached schedule v{schedule.version} "
            f"({len(schedule.weekly_rules)} weekly rule(s), "
            f"{len(schedule.exceptions)} exception(s), tz={schedule.timezone_name})"
        )

    # Layered on top, so an operator's own programme survives a reboot the same
    # way the published one does.
    await restore_local_schedule(state)


async def restore_local_schedule(state: RuntimeState) -> None:
    """
    Reload a programme edited on this device.

    Kept only while it still sits on the published version it was made against:
    if the server published something newer while this device was off, that
    publish outranks the edits, exactly as a live push would.
    """
    from schedule_editor import clear_local_schedule, load_local_schedule

    local, error = await load_local_schedule()

    if error:
        await state.log(
            f"[schedule] Ignoring unusable local schedule ({error})",
            level=logging.WARNING,
        )
        return
    if local is None:
        return

    if local.based_on_version != schedule_runner.server_version:
        await clear_local_schedule()
        await state.log(
            f"[schedule] Discarded local edits made on v{local.based_on_version} — "
            f"the server has since published v{schedule_runner.server_version}",
            level=logging.WARNING,
        )
        return

    try:
        schedule = await schedule_runner.restore_local(local, state)
    except ScheduleError as exc:
        await clear_local_schedule()
        await state.log(
            f"[schedule] Discarded unusable local edits ({exc}) — "
            "back to the published schedule",
            level=logging.WARNING,
        )
        return

    await state.log(
        f"[schedule] Restored locally edited schedule v{schedule.version} "
        f"(revision {local.revision}, saved {local.saved_at or 'unknown'}; "
        f"{len(schedule.weekly_rules)} weekly rule(s), "
        f"{len(schedule.exceptions)} exception(s)) — "
        "the server's next publish replaces it",
        level=logging.WARNING,
    )


async def restore_cached_device_record(state: RuntimeState) -> None:
    """
    Reload the last device record the server gave us.

    Calibration offsets and sensor enable flags have to survive a reboot with
    no network, or a device that comes up during an outage would silently
    report uncalibrated readings — and feed them to the limit guard.
    """
    record, error = await load_cached_device_record()

    if error:
        await state.log(
            f"[device] Ignoring unusable record cache ({error}) — will re-fetch",
            level=logging.WARNING,
        )
        return
    if record is None:
        await state.log("[device] No cached device record — will fetch after login")
        return

    device_record_store.set_record(record)
    await state.log(
        f"[device] Restored cached record for {record.public_id} "
        f"({len(record.sensors)} server sensor(s), "
        f"{len(device_record_store.offsets)} calibration offset(s), "
        f"{len(device_record_store.disabled)} disabled)"
    )


async def device_record_loop(state: RuntimeState) -> None:
    """
    Keep the device record fresh.

    An installer can change calibration or disable a probe at any time, and
    nothing pushes that over the WebSocket — it only appears in the record, so
    it has to be polled. Slowly: it is a handful of fields that change rarely.
    """
    if not await state.wait_authenticated():
        return

    while not state.shutdown.is_set():
        # Shared with the post-hello reconciler, which fetches a record too.
        # Adoption is not atomic, so without this the two could apply halves of
        # two different records.
        async with state.record_lock:
            try:
                record, payload = await fetch_device_record()
            except DeviceRecordError as exc:
                await state.log(
                    f"[device] Server record unusable: {exc}", level=logging.WARNING
                )
            except Exception as exc:
                await state.log(
                    f"[device] Failed to fetch record: {exc}", level=logging.WARNING
                )
            else:
                await adopt_device_record(state, record, payload)

        # Wakes early when something asks for a refresh — reporting modes
        # upstream does, because this poll is what reconciles them.
        state.record_refresh_requested.clear()
        waiters = [
            asyncio.create_task(state.shutdown.wait()),
            asyncio.create_task(state.record_refresh_requested.wait()),
        ]
        try:
            await asyncio.wait(
                waiters,
                timeout=DEVICE_RECORD_REFRESH_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()


async def restore_cached_modes(state: RuntimeState) -> None:
    """
    Reload which units were left in manual.

    Restored before the first schedule evaluation, so a unit an operator took
    off the programme stays off it across a reboot instead of being handed
    straight back to the schedule.
    """
    from setpoint_store import load_setpoints

    setpoints = await load_setpoints()
    if setpoints:
        await state.restore_setpoints(setpoints)
        await state.log(
            "[setpoint] Restored "
            + ", ".join(
                f"boiler {i} at {e.temperature_c:.1f} °C"
                + ("" if e.published else " (not yet published)")
                for i, e in sorted(setpoints.items())
            )
        )

    modes = await load_modes()
    if not modes:
        await state.log("[mode] No saved modes — every unit starts on automatic")
        return

    await state.restore_modes(modes)

    manual = sorted(str(t) for t, e in modes.items() if e.mode == "manual")
    if manual:
        await state.log(
            f"[mode] Restored {len(modes)} mode(s); still on manual: {', '.join(manual)}"
        )
    else:
        await state.log(f"[mode] Restored {len(modes)} mode(s), all automatic")

    unreported = sorted(str(t) for t, e in modes.items() if not e.published)
    if unreported:
        await state.log(
            f"[mode] Not yet reported to the server: {', '.join(unreported)} — "
            "will be sent on the next connection"
        )


async def restore_cached_config(state: RuntimeState) -> None:
    """
    Reload the last config the server pushed.

    Without this a restart comes up with no limits and no telemetry cadence
    until the WebSocket reconnects — which may be never, if the network is down.
    Reporting the cached version in device.hello also stops the server
    re-pushing an unchanged document on every boot.
    """
    config, document, error = await load_cached_config()

    if error:
        await state.log(
            f"[config] Ignoring unusable cache ({error}) — waiting for server push",
            level=logging.WARNING,
        )
    elif config is None or document is None:
        await state.log("[config] No cached config — waiting for server push")
    else:
        config_store.set_server_document(document)
        await state.set_device_config(config)
        await state.set_active_config_version(config.version)
        await state.log(
            f"[config] Restored cached config v{config.version} "
            f"(telemetry every {config.telemetry_interval_seconds}s)"
        )

    # Layered on top, so limits an operator set by hand survive a reboot.
    await restore_local_config(state)


async def restore_local_config(state: RuntimeState) -> None:
    """
    Reload limits edited on this device.

    Kept only while they still sit on the published version they were made
    against: if the server published a newer config while the device was off,
    that publish outranks them, exactly as a live push would.
    """
    from config_editor import clear_local_config, load_local_config

    local, error = await load_local_config()

    if error:
        await state.log(
            f"[config] Ignoring unusable local limits ({error})",
            level=logging.WARNING,
        )
        return
    if local is None:
        return

    if local.based_on_version != config_store.server_version:
        await clear_local_config()
        await state.log(
            f"[config] Discarded local limits set on v{local.based_on_version} — "
            f"the server has since published v{config_store.server_version}",
            level=logging.WARNING,
        )
        return

    try:
        config = await config_store.restore_local(local, state)
    except ConfigError as exc:
        await clear_local_config()
        await state.log(
            f"[config] Discarded unusable local limits ({exc}) — "
            "back to the published config",
            level=logging.WARNING,
        )
        return

    await state.log(
        f"[config] Restored locally edited limits on v{config.version} "
        f"(revision {local.revision}, saved {local.saved_at or 'unknown'}): "
        f"water {config.limits.min_water_temperature_c}–"
        f"{config.limits.max_water_temperature_c} °C, "
        f"ambient max {config.limits.max_ambient_temperature_c} °C — "
        "the server's next publish replaces them",
        level=logging.WARNING,
    )


def install_signal_handlers(state: RuntimeState) -> None:
    """
    Ask the loop to shut down cleanly on SIGTERM/SIGINT.

    systemd stops a service with SIGTERM. Without a handler Python would exit
    immediately, skipping relay cleanup and leaving the SQLite connection open.
    Not available on Windows, where the loop has no signal support.
    """
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        signal_number = getattr(signal, name, None)
        if signal_number is None:
            continue
        try:
            loop.add_signal_handler(signal_number, state.shutdown.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: KeyboardInterrupt handling covers Ctrl-C


async def main() -> None:
    configure_logging()
    state = RuntimeState(read_interval=DEFAULT_READ_INTERVAL)
    install_signal_handlers(state)

    # Constructed here, started by the control menu — it is the only thing that
    # reads from it, and it has to be able to fall back to the keyboard when a
    # keypad will not come up. A misconfigured pin map is caught in the
    # constructor and must not stop an agent whose real job is the heating.
    try:
        state.keypad = Keypad()
    except Exception as exc:
        await state.log(
            f"[menu] Keypad not configured: {exc} — the menu falls back to "
            "the keyboard, if there is one",
            level=logging.ERROR,
        )

    # The record comes first: it is the mapping source, so it has to be in
    # place before the mapping is built from it.
    await restore_cached_device_record(state)
    await load_mapping(state)
    await restore_cached_config(state)
    await restore_cached_schedule(state)
    # Before any task starts, so the first schedule tick already knows which
    # units are hands-off.
    await restore_cached_modes(state)
    await print_startup_banner(state)

    auth_task = asyncio.create_task(auth_loop(state), name="auth_loop")
    sensor_task = asyncio.create_task(sensor_loop(state), name="sensor_loop")
    menu_task = asyncio.create_task(run_control_menu(state), name="control_menu")
    ws_task = asyncio.create_task(run_websocket_client(state), name="websocket_client")
    schedule_task = asyncio.create_task(schedule_loop(state), name="schedule_loop")
    record_task = asyncio.create_task(device_record_loop(state), name="device_record")
    publish_task = asyncio.create_task(run_state_publisher(state), name="state_publisher")
    tasks = (
        auth_task,
        sensor_task,
        menu_task,
        ws_task,
        schedule_task,
        record_task,
        publish_task,
    )

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        state.shutdown.set()
    finally:
        state.shutdown.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await state.log("Shutdown complete.")
        shutdown_logging()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # main() has already stopped the logging listener by this point, so a
        # log call here would go nowhere.
        print("\nStopping...")
