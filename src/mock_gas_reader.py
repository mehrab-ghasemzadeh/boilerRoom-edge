import asyncio
import random

from config import GAS_SENSORS


class MockGasReader:

    def __init__(self):
        self.values = {
            sensor_id: random.randint(8000, 15000)
            for sensor_id in GAS_SENSORS
        }

    async def start(self):
        pass

    async def _read_sensor(self, sensor_id: int):
        await asyncio.sleep(0.2)

        self.values[sensor_id] += random.randint(-100, 100)
        self.values[sensor_id] = max(
            0,
            min(32767, self.values[sensor_id]),
        )
        return self.values[sensor_id]

    async def read_all(self):
        results = await asyncio.gather(*[
            self._read_sensor(sensor_id)
            for sensor_id in GAS_SENSORS
        ])
        return dict(zip(GAS_SENSORS.keys(), results))

    async def close(self):
        pass
