import asyncio
from pathlib import Path

from config import ONE_WIRE_PATH, TEMPERATURE_SENSORS


class TemperatureReader:

    def __init__(self):
        self.base_path = Path(ONE_WIRE_PATH)

    async def start(self):
        pass

    def _read_sensor(self, physical_id: str):
        device = self.base_path / physical_id / "w1_slave"

        if not device.exists():
            return None

        for _ in range(5):
            lines = device.read_text().splitlines()

            if lines[0].endswith("YES"):
                pos = lines[1].find("t=")

                if pos != -1:
                    return round(
                        float(lines[1][pos + 2:]) / 1000,
                        2,
                    )

        return None

    async def read_all(self):
        items = list(TEMPERATURE_SENSORS.items())
        results = await asyncio.gather(*[
            asyncio.to_thread(self._read_sensor, cfg["physical_id"])
            for _, cfg in items
        ])
        return {
            sensor_id: value
            for (sensor_id, _), value in zip(items, results)
        }

    async def close(self):
        pass
