"""
Mapping data providers.

The device record on the server is the source of truth for the installation:
which probes and relays exist, how they are wired, and which boiler or pump
each belongs to. ``RecordMappingProvider`` converts that record into a mapping,
and its on-disk cache is what the device boots from.

There is no local mapping file any more. ``FileMappingProvider`` still exists
for bench work behind ``BOILERROOM_MAPPING_SOURCE=file``, but nothing falls
back to it: a device that cannot describe its own wiring must wait to be told,
not run on a stale local guess.
"""

from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

from logging_setup import get_logger

_log = get_logger("mapping")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAPPING_PATH = PROJECT_ROOT / "mapping.json"

DEFAULT_SOURCE = "record"


class MappingProvider(ABC):
    """Fetches raw mapping data (same structure as mapping.json)."""

    @abstractmethod
    async def fetch(self) -> dict:
        ...


class FileMappingProvider(MappingProvider):
    """Load mapping from a local JSON file."""

    def __init__(self, path: Path | None = None):
        override = os.environ.get("BOILERROOM_MAPPING")
        self.path = path or (Path(override) if override else DEFAULT_MAPPING_PATH)

    async def fetch(self) -> dict:
        return await asyncio.to_thread(self._read_file)

    def _read_file(self) -> dict:
        if not self.path.exists():
            raise FileNotFoundError(
                f"Mapping file not found: {self.path}. "
                "The default mapping source is the server device record; set "
                "BOILERROOM_MAPPING to a file only for bench work."
            )

        with open(self.path, encoding="utf-8") as f:
            return json.load(f)


class MappingUnavailableError(RuntimeError):
    """The device record cannot yet describe this installation."""


class RecordMappingProvider(MappingProvider):
    """
    Build the mapping from the server's device record.

    At startup there is no session yet — login runs as a background task — so
    the cached record is used, which is what lets a paired device boot with no
    network at all. A device that has never been paired has nothing to go on
    and raises ``MappingUnavailableError``; the agent then waits for the record
    loop to fetch one rather than failing to start.
    """

    async def fetch(self) -> dict:
        from device_record import device_record_store, load_cached_device_record
        from record_mapping import RecordMappingError, mapping_from_record, readiness_problems

        record = device_record_store.record
        origin = "active record"
        if record is None:
            record, error = await load_cached_device_record()
            origin = "cached record"
            if error:
                raise MappingUnavailableError(f"cached record is unusable ({error})")

        if record is None:
            raise MappingUnavailableError("no device record available yet")

        problems = readiness_problems(record)
        if problems:
            raise MappingUnavailableError(
                f"{origin} is incomplete: " + "; ".join(problems)
            )

        try:
            document = mapping_from_record(record)
        except RecordMappingError as exc:
            raise MappingUnavailableError(f"{origin} cannot be mapped: {exc}") from exc

        _log.info(
            "Mapping built from the %s: %d temperature sensor(s), %d gas sensor(s), "
            "%d relay(s), %d unit(s)",
            origin,
            len(document["temperature_sensors"]),
            len(document["gas_sensors"]),
            len(document["relays"]),
            len(document["units"]),
        )
        return document


def create_mapping_provider() -> MappingProvider:
    source = os.environ.get("BOILERROOM_MAPPING_SOURCE", DEFAULT_SOURCE).lower()

    if source == "file":
        return FileMappingProvider()
    if source in ("record", "server"):
        return RecordMappingProvider()

    raise ValueError(
        f"Unknown BOILERROOM_MAPPING_SOURCE={source!r}. "
        "Valid values: 'record' (default), 'file'."
    )
