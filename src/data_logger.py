import asyncio
import csv
import datetime
import pathlib

from config import GAS_SENSORS, TEMPERATURE_SENSORS


def _save_readings_sync(temperatures, gas):
    data_dir = pathlib.Path("data")
    data_dir.mkdir(exist_ok=True)

    now = datetime.datetime.now().isoformat(timespec="seconds").replace(":", "-")
    filename = data_dir / f"{now}.csv"

    rows = []

    for sensor_id, value in temperatures.items():
        cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
        rows.append({
            "sensor_type": "temperature",
            "sensor_id": sensor_id,
            "sensor_name": cfg.get("name", f"Sensor {sensor_id}"),
            "role": cfg.get("role", ""),
            "unit": cfg.get("unit", ""),
            "value": round(value, 2) if value is not None else "",
            "unit_of_measure": "°C",
        })

    for sensor_id, value in gas.items():
        cfg = GAS_SENSORS.get(sensor_id, {})
        rows.append({
            "sensor_type": "gas",
            "sensor_id": sensor_id,
            "sensor_name": cfg.get("name", f"Sensor {sensor_id}"),
            "role": cfg.get("role", ""),
            "unit": cfg.get("unit", ""),
            "value": value,
            "unit_of_measure": "ADC counts",
        })

    fieldnames = [
        "sensor_type",
        "sensor_id",
        "sensor_name",
        "role",
        "unit",
        "value",
        "unit_of_measure",
    ]

    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


async def save_readings(temperatures, gas):
    """Save sensor readings to data/<timestamp>.csv without blocking the event loop."""
    await asyncio.to_thread(_save_readings_sync, temperatures, gas)
