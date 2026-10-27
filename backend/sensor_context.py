from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def _sensor_map() -> dict:
    path = ROOT / "artifacts" / "sensor_map.json"
    return json.loads(path.read_text(encoding="utf-8"))["sensors"]


@lru_cache(maxsize=1)
def _profiles() -> dict[int, dict]:
    path = ROOT / "Data" / "reference" / "profile_metadata.parquet"
    frame = pd.read_parquet(path)
    return {
        int(row["cycle_id"]): {
            "cooler": float(row["cooler"]),
            "valve": float(row["valve"]),
            "pump": float(row["pump"]),
            "accumulator": float(row["accumulator"]),
            "stable_flag": int(row["stable_flag"]),
            "context_only": True,
            "note": "Documented cycle condition context only; not a sensor diagnosis.",
        }
        for row in frame.to_dict(orient="records")
    }


def annotate_point(point: dict) -> dict:
    sensor_id = point.get("sensor_id")
    sensor = _sensor_map().get(sensor_id, {})
    location_area = sensor.get("location_area", "not documented")
    cycle_id = point.get("cycle_id")
    context = _profiles().get(int(cycle_id)) if cycle_id is not None else None
    return {
        **point,
        "location_area": location_area,
        "component_context": context,
        "process_event_warning": None,
        "location_statement": (
            f"data fault located at sensor {sensor_id} (area {location_area}); "
            "this is a signal-quality finding, not a diagnosis of a failed component"
        ),
    }


def annotate_coincident_events(points: list[dict]) -> list[dict]:
    grouped: dict[tuple[int, int, str], list[int]] = {}
    for index, point in enumerate(points):
        area = point.get("location_area")
        if (
            area
            and area not in {"not documented", "not applicable"}
            and point.get("fault_type") not in {None, "healthy"}
        ):
            key = (
                int(point["cycle_id"]),
                int(point["second_in_cycle"]),
                str(area),
            )
            grouped.setdefault(key, []).append(index)
    for indices in grouped.values():
        if len({points[index].get("sensor_id") for index in indices}) > 1:
            for index in indices:
                points[index]["process_event_warning"] = (
                    "possible genuine process event - verify"
                )
    return points


def describe_sensor(sensor_id: str, cycle_id: int) -> dict:
    sensor = _sensor_map().get(sensor_id, {})
    return {
        "location_area": sensor.get("location_area", "not documented"),
        "component_context": _profiles().get(int(cycle_id)),
        "location_statement": (
            f"data fault located at sensor {sensor_id} "
            f"(area {sensor.get('location_area', 'not documented')}); "
            "this is a signal-quality finding, not a diagnosis of a failed component"
        ),
    }
