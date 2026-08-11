import asyncio
import random

from config import TEMPERATURE_SENSORS

# Plausible ranges per role, so simulated readings exercise the config limits
# realistically. A flat 20-80 °C across every sensor would put the indoor
# ambient probe over max_ambient_temperature_c most of the time and cut the
# boilers on nearly every cycle.
ROLE_RANGES = {
    "environment_inside": (18.0, 28.0),
    "environment_outside": (5.0, 35.0),
    "boiler_input_water": (30.0, 60.0),
    "boiler_output_water": (40.0, 77.0),
    "boiler_body": (40.0, 79.0),
}


class MockTemperatureReader:

    def __init__(
        self,
        min_temp: float = 20.0,
        max_temp: float = 79.0,
        read_delay: float = 0.35,
    ):
        self.min_temp = min_temp
        self.max_temp = max_temp
        self.read_delay = read_delay

    async def start(self):
        pass

    def _range_for(self, sensor_id: int) -> tuple[float, float]:
        role = TEMPERATURE_SENSORS.get(sensor_id, {}).get("role")
        return ROLE_RANGES.get(role, (self.min_temp, self.max_temp))

    async def _read_sensor(self, sensor_id: int):
        await asyncio.sleep(self.read_delay)
        low, high = self._range_for(sensor_id)
        return round(random.uniform(low, high), 2)

    async def read_all(self):
        items = list(TEMPERATURE_SENSORS.items())
        results = await asyncio.gather(*[
            self._read_sensor(sensor_id)
            for sensor_id, _ in items
        ])
        return {
            sensor_id: value
            for (sensor_id, _), value in zip(items, results)
        }

    async def close(self):
        pass
