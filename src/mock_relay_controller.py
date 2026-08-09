import asyncio

from config import RELAYS


class MockRelayController:

    def __init__(self):
        self._states = {relay_id: False for relay_id in RELAYS}

    async def start(self):
        pass

    async def turn_on(self, relay_id: int):
        if relay_id not in RELAYS:
            raise ValueError(f"Invalid relay ID: {relay_id}")
        self._states[relay_id] = True
        await asyncio.sleep(0)

    async def turn_off(self, relay_id: int):
        if relay_id not in RELAYS:
            raise ValueError(f"Invalid relay ID: {relay_id}")
        self._states[relay_id] = False
        await asyncio.sleep(0)

    async def toggle(self, relay_id: int):
        current = self._states.get(relay_id, False)
        self._states[relay_id] = not current
        await asyncio.sleep(0)

    def get_state(self, relay_id: int) -> bool:
        return self._states.get(relay_id, False)

    async def cleanup(self):
        self._states = {relay_id: False for relay_id in RELAYS}
