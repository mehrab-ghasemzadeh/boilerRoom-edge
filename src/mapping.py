"""
Device mapping — public API.

Mappings are loaded at runtime via ``await initialize_mapping()``. The default
provider builds the mapping from the server's device record. There is no
local mapping file; an unpaired device waits for the server to describe it.

Environment variables:
  BOILERROOM_MAPPING_SOURCE  "record" (default) or "file"
  BOILERROOM_MAPPING         mapping file path, used only when source=file
"""

from mapping_schema import (
    DeviceMapping,
    find_relay,
    find_temperature_sensor,
    parse_mapping,
    relays_for_unit,
    temperature_sensors_for_unit,
)
from mapping_store import initialize_mapping, mapping_store, reload_mapping

__all__ = [
    "DeviceMapping",
    "find_relay",
    "find_temperature_sensor",
    "get_mapping",
    "initialize_mapping",
    "mapping_store",
    "parse_mapping",
    "reload_mapping",
    "relays_for_unit",
    "temperature_sensors_for_unit",
]


def get_mapping() -> DeviceMapping:
    return mapping_store.get()
