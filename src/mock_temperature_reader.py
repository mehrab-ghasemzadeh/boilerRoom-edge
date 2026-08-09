import asyncio
import random

from config import TEMPERATURE_SENSORS


class MockTemperatureReader:

    def __init__(
        self,
        min_temp: float = 20.0,
        max_temp: float = 80.0,
        read_delay: float = 0.35,
    ):
        self.min_temp = min_temp
        self.max_temp = max_temp
        self.read_delay = read_delay

    async def start(self):
        pass

    async def _read_sensor(self, sensor_id: int):
        await asyncio.sleep(self.read_delay)
        return round(
            random.uniform(self.min_temp, self.max_temp),
            2,
        )

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
