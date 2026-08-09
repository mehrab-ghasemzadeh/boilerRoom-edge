from load_env import load_dotenv

load_dotenv()

import asyncio

from auth import login_sync
from config import load_device_mapping
from control_menu import run_control_menu
from data_logger import save_readings
from errors_client import error_reporter
from runtime_state import RuntimeState
from telemetry_client import post_telemetry
from ws_client import run_websocket_client

# ----------------------------------------------------
# Configuration
# ----------------------------------------------------

USE_MOCK_HARDWARE = True
DEFAULT_READ_INTERVAL = 10

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

    await state.log("========================================")
    await state.log(" Boiler Room Monitoring System Started")
    await state.log("========================================\n")

    await state.log("Equipment units:")
    for unit_id, unit in UNITS.items():
        await state.log(f"  {unit_id}: {unit['name']}")
    await state.log("")

    await state.log("Temperature sensor mapping:")
    for sid, cfg in sorted(TEMPERATURE_SENSORS.items()):
        unit = cfg.get("unit") or "—"
        await state.log(
            f"  Sensor {sid}: {cfg['name']}  (role={cfg['role']}, unit={unit})"
        )
    await state.log("")

    await state.log("Relay mapping:")
    for rid, cfg in sorted(RELAYS.items()):
        unit = cfg.get("unit") or "—"
        await state.log(
            f"  Relay {rid}: {cfg['name']}  "
            f"(role={cfg['role']}, unit={unit}, GPIO {cfg['gpio']})"
        )
    await state.log("")


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

            try:
                await post_telemetry(temperatures, gas, relay_controller)
            except Exception as exc:
                await state.log(f"[telemetry] Failed to post: {exc}")

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
            return_exceptions=True,
        )


async def main() -> None:
    state = RuntimeState(read_interval=DEFAULT_READ_INTERVAL)

    await load_device_mapping()
    await print_startup_banner(state)

    sensor_task = asyncio.create_task(sensor_loop(state), name="sensor_loop")
    menu_task = asyncio.create_task(run_control_menu(state), name="control_menu")
    ws_task = asyncio.create_task(run_websocket_client(state), name="websocket_client")

    try:
        await asyncio.gather(sensor_task, menu_task, ws_task)
    except asyncio.CancelledError:
        state.shutdown.set()
    finally:
        state.shutdown.set()
        for task in (sensor_task, menu_task, ws_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(sensor_task, menu_task, ws_task, return_exceptions=True)
        await state.log("Shutdown complete.")


if __name__ == "__main__":
    login_sync()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopping...")
