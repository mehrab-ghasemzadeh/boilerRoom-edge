"""
Mapping data providers.

Today mappings come from mapping.json on disk. Later a server provider will
fetch the same JSON shape over the network (see ServerMappingProvider).
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAPPING_PATH = PROJECT_ROOT / "mapping.json"


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
        import asyncio

        return await asyncio.to_thread(self._read_file)

    def _read_file(self) -> dict:
        if not self.path.exists():
            raise FileNotFoundError(
                f"Mapping file not found: {self.path}. "
                "Copy mapping.json to the project root and edit it for your installation."
            )

        with open(self.path, encoding="utf-8") as f:
            return json.load(f)


class ServerMappingProvider(MappingProvider):
    """
    Fetch mapping from a remote server.

    Not implemented yet — placeholder for future work. The server is expected
    to return the same JSON document shape as mapping.json.
    """

    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("BOILERROOM_MAPPING_URL", "")

    async def fetch(self) -> dict:
        raise NotImplementedError(
            "Server mapping provider is not implemented yet. "
            "Use BOILERROOM_MAPPING_SOURCE=file (default) or set mapping.json locally."
        )


def create_mapping_provider() -> MappingProvider:
    source = os.environ.get("BOILERROOM_MAPPING_SOURCE", "file").lower()

    if source == "file":
        return FileMappingProvider()
    if source == "server":
        return ServerMappingProvider()

    raise ValueError(
        f"Unknown BOILERROOM_MAPPING_SOURCE={source!r}. Valid values: 'file', 'server'."
    )
