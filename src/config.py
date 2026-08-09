"""
System configuration.

Device mapping is loaded at startup via ``await load_device_mapping()``.
Until then, sensor/relay dicts below are empty.

Source is selected by BOILERROOM_MAPPING_SOURCE (file now, server later).
"""

# GPIO pin used by the 1-Wire bus
ONE_WIRE_GPIO = 4

# Linux 1-Wire device directory
ONE_WIRE_PATH = "/sys/bus/w1/devices"

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED = 1_000_000

# Populated by load_device_mapping() at startup
UNITS: dict[str, dict[str, str]] = {}
TEMPERATURE_SENSORS: dict[int, dict] = {}
GAS_SENSORS: dict[int, dict] = {}
RELAYS: dict[int, dict] = {}


async def load_device_mapping():
    """Fetch mapping from the configured provider and sync into module-level dicts."""
    from mapping_store import mapping_store

    device_mapping = await mapping_store.load()

    UNITS.clear()
    UNITS.update(device_mapping.units)

    TEMPERATURE_SENSORS.clear()
    TEMPERATURE_SENSORS.update(device_mapping.temperature_sensors)

    GAS_SENSORS.clear()
    GAS_SENSORS.update(device_mapping.gas_sensors)

    RELAYS.clear()
    RELAYS.update(device_mapping.relays)

    return device_mapping
