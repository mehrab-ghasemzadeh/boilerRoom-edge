"""
Device mapping — public API.

Mappings are loaded at runtime via ``await initialize_mapping()``. The default
provider reads mapping.json; a server provider will be added later.

Environment variables:
  BOILERROOM_MAPPING_SOURCE  "file" (default) or "server"
  BOILERROOM_MAPPING         path to mapping.json when source=file
  BOILERROOM_MAPPING_URL     server URL when source=server (not implemented)
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
