from load_env import load_dotenv

load_dotenv()

import asyncio
import logging
import signal
import time

from auth import AuthError, MissingCredentialsError, token_manager
from config import load_device_mapping
from control_menu import run_control_menu
from data_logger import reading_store, save_readings
from device_config import load_cached_config
from errors_client import error_reporter
from limits_guard import limit_guard
from logging_setup import configure_logging, shutdown_logging
from runtime_state import RuntimeState
from schedule_runner import load_cached_schedule, schedule_runner
from telemetry_client import post_telemetry
from ws_client import run_websocket_client

# ----------------------------------------------------
# Configuration
# ----------------------------------------------------

USE_MOCK_HARDWARE = True
DEFAULT_READ_INTERVAL = 10

# How often to re-check the active schedule for a state transition
SCHEDULE_TICK_SECONDS = 20

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
else:
    from temperature_reader import TemperatureReader
    from gas_reader import GasReader
    from relay_controller import RelayController


async def print_startup_banner(state: RuntimeState) -> None:
    from config import GAS_SENSORS, RELAYS, TEMPERATURE_SENSORS, UNITS

    await state.echo("========================================")
    await state.echo(" Boiler Room Monitoring System Started")
    await state.echo("========================================\n")

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

    last_telemetry_at: float | None = None
    offline_notice_shown = False

    try:
        while not state.shutdown.is_set():
            temperatures, gas = await asyncio.gather(
                temperature_reader.read_all(),
                gas_reader.read_all(),
            )

            await state.update_readings(temperatures, gas)
            await save_readings(temperatures, gas)

            await error_reporter.check_temperature_faults(
                temperatures,
                log=state.log,
            )

            # Enforce config limits before telemetry, so a cut is reported in
            # the same cycle it happens.
            await limit_guard.check(state, temperatures)

            # config.apply may ask for a slower cadence than the read interval;
            # without it, post every cycle as before.
            telemetry_interval = await state.get_telemetry_interval()
            now = time.monotonic()
            due = (
                last_telemetry_at is None
                or telemetry_interval is None
                or now - last_telemetry_at >= telemetry_interval
            )

            if due and not state.authenticated.is_set():
                if not offline_notice_shown:
                    offline_notice_shown = True
                    await state.log("[telemetry] Offline — holding until a session exists")
            elif due:
                offline_notice_shown = False
                last_telemetry_at = now
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
    schedule, error = await load_cached_schedule()

    if error:
        await state.log(
            f"[schedule] Ignoring unusable cache ({error}) — waiting for server push",
            level=logging.WARNING,
        )
        return
    if schedule is None:
        await state.log("[schedule] No cached schedule — waiting for server push")
        return

    schedule_runner.set_schedule(schedule)
    await state.set_active_schedule_version(schedule.version)
    await state.log(
        f"[schedule] Restored cached schedule v{schedule.version} "
        f"({len(schedule.weekly_rules)} weekly rule(s), "
        f"{len(schedule.exceptions)} exception(s), tz={schedule.timezone_name})"
    )


async def restore_cached_config(state: RuntimeState) -> None:
    """
    Reload the last config the server pushed.

    Without this a restart comes up with no limits and no telemetry cadence
    until the WebSocket reconnects — which may be never, if the network is down.
    Reporting the cached version in device.hello also stops the server
    re-pushing an unchanged document on every boot.
    """
    config, error = await load_cached_config()

    if error:
        await state.log(
            f"[config] Ignoring unusable cache ({error}) — waiting for server push",
            level=logging.WARNING,
        )
        return
    if config is None:
        await state.log("[config] No cached config — waiting for server push")
        return

    await state.set_device_config(config)
    await state.set_active_config_version(config.version)
    await state.log(
        f"[config] Restored cached config v{config.version} "
        f"(telemetry every {config.telemetry_interval_seconds}s)"
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

    await load_device_mapping()
    await restore_cached_config(state)
    await restore_cached_schedule(state)
    await print_startup_banner(state)

    auth_task = asyncio.create_task(auth_loop(state), name="auth_loop")
    sensor_task = asyncio.create_task(sensor_loop(state), name="sensor_loop")
    menu_task = asyncio.create_task(run_control_menu(state), name="control_menu")
    ws_task = asyncio.create_task(run_websocket_client(state), name="websocket_client")
    schedule_task = asyncio.create_task(schedule_loop(state), name="schedule_loop")
    tasks = (auth_task, sensor_task, menu_task, ws_task, schedule_task)

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
