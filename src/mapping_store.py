"""
Runtime mapping store.

Loads device mapping through a MappingProvider, validates it, and holds the
active configuration. Call ``await initialize_mapping()`` at startup; later
``await reload_mapping()`` can refresh from the server without restarting.
"""

from __future__ import annotations

from mapping_provider import MappingProvider, create_mapping_provider
from mapping_schema import DeviceMapping, parse_mapping


class MappingStore:
    def __init__(self, provider: MappingProvider | None = None):
        self._provider = provider or create_mapping_provider()
        self._mapping: DeviceMapping | None = None

    @property
    def is_loaded(self) -> bool:
        return self._mapping is not None

    @property
    def provider(self) -> MappingProvider:
        return self._provider

    def get(self) -> DeviceMapping:
        if self._mapping is None:
            raise RuntimeError(
                "Device mapping is not loaded. "
                "Call await initialize_mapping() before using config."
            )
        return self._mapping

    async def load(self) -> DeviceMapping:
        raw = await self._provider.fetch()
        self._mapping = parse_mapping(raw)
        return self._mapping

    async def reload(self) -> DeviceMapping:
        """Re-fetch mapping from the provider (e.g. after a server update)."""
        return await self.load()


mapping_store = MappingStore()


async def initialize_mapping() -> DeviceMapping:
    return await mapping_store.load()


async def reload_mapping() -> DeviceMapping:
    return await mapping_store.reload()
