import asyncio

import RPi.GPIO as GPIO

from config import RELAYS


class RelayController:
    """
    Controls up to 8 relays (active-low) using RPi.GPIO.
    All hardware access runs off the event loop via asyncio.to_thread.
    """

    def __init__(self):
        self._states = {relay_id: False for relay_id in RELAYS}

    async def start(self):
        await asyncio.to_thread(self._setup_gpio)

    def _setup_gpio(self):
        GPIO.setmode(GPIO.BCM)
        # The keypad claims its own pins from another thread; without this,
        # whichever of the two runs second warns about every channel the first
        # has already set up.
        GPIO.setwarnings(False)
        for relay in RELAYS.values():
            gpio = relay["gpio"]
            GPIO.setup(gpio, GPIO.OUT)
            GPIO.output(gpio, GPIO.HIGH)
        self._states = {relay_id: False for relay_id in RELAYS}

    def _set_relay(self, relay_id: int, on: bool):
        if relay_id not in RELAYS:
            raise ValueError(f"Invalid relay ID: {relay_id}")
        gpio = RELAYS[relay_id]["gpio"]
        GPIO.output(gpio, GPIO.LOW if on else GPIO.HIGH)
        self._states[relay_id] = on

    async def turn_on(self, relay_id: int):
        await asyncio.to_thread(self._set_relay, relay_id, True)

    async def turn_off(self, relay_id: int):
        await asyncio.to_thread(self._set_relay, relay_id, False)

    async def toggle(self, relay_id: int):
        current = self._states.get(relay_id, False)
        await asyncio.to_thread(self._set_relay, relay_id, not current)

    def get_state(self, relay_id: int) -> bool:
        return self._states.get(relay_id, False)

    async def cleanup(self):
        await asyncio.to_thread(self._cleanup_sync)

    def _cleanup_sync(self):
        for relay_id in RELAYS:
            self._set_relay(relay_id, False)
        # Only the relay pins. A bare GPIO.cleanup() releases every channel on
        # the chip, which since the keypad arrived would also drop the pins it
        # is scanning — and the sensor loop's shutdown runs while the menu is
        # still up.
        GPIO.cleanup([relay["gpio"] for relay in RELAYS.values()])
