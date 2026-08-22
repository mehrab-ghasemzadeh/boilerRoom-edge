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

# BCM pins the gas ADC occupies: MOSI, SCLK, CE0.
# NOTE: GPIO 11 (SCLK) is also used by the keypad — a hardware conflict if
# both are active. The keypad takes precedence; the gas ADC must not be used
# on this wiring, or the keypad must move off GPIO 11.
SPI_GPIO = (10, 11, 8)

# ST7920 128x64 graphical LCD.
#
# Wired in parallel mode (PSB tied to GND):
#   PSB -> GPIO 6 (GND), GND -> GPIO 9, VCC -> GPIO 2
#   RS  -> GPIO 24, R/W -> GPIO 19, E -> GPIO 23
#
# The current display.py only supports SPI mode. A parallel-mode driver is
# needed for this wiring, or rewire the panel to SPI (PSB to VCC, connect
# SID/CLK/CS).
DISPLAY_SPI_BUS = 0
DISPLAY_SPI_DEVICE = 1  # CE1
DISPLAY_SPI_SPEED = 800_000

# BCM pins the panel occupies in SPI mode: SID -> MOSI, CLK -> SCLK, CS -> CE1.
# In parallel mode these are not used; the actual pins are RS=24, R/W=19, E=23.
DISPLAY_GPIO = (10, 11, 7)

# Parallel-mode control pins for conflict detection when PSB is tied LOW.
DISPLAY_PARALLEL_GPIO = (24, 19, 23, 6)

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
