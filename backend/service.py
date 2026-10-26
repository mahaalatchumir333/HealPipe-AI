from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backend.database import insert_run, list_runs
from pipeline.calibrate import ARTIFACT, apply_calibrated_confidence, calibrate_run
from pipeline.detect import detect_faults
from pipeline.reconstruct import SENSORS, ensure_models, load_model_map

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "Data"


def _json_records(frame: pd.DataFrame) -> list[dict]:
    def json_value(value):
        if isinstance(value, dict):
            return {key: json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_value(item) for item in value]
        if not pd.api.types.is_scalar(value):
            return json_value(value.tolist() if hasattr(value, "tolist") else list(value))
        if pd.isna(value):
            return None
        return value.item() if hasattr(value, "item") else value

    records = []
    for row in frame.to_dict(orient="records"):
        records.append({key: json_value(value) for key, value in row.items()})
    return records


def load_partition_data(partition: str = "validation") -> pd.DataFrame:
    if partition not in {"validation", "test"}:
        raise ValueError("partition must be 'validation' or 'test'")
    return pd.read_parquet(DATA_DIR / "corrupted" / f"{partition}_corrupted.parquet")


def current_metrics() -> dict:
    data = load_partition_data("validation")
    total_sensors = len([c for c in data.columns if c not in {"cycle_id", "second_in_cycle"}])
    total_channels = len(data) * total_sensors
    missing_points = int(data[SENSORS].isna().sum().sum())
    return {
        "total_cycles": int(data["cycle_id"].nunique()),
        "total_samples": int(len(data)),
        "channels": total_sensors,
        "total_points": total_channels,
        "missing_points": missing_points,
        "missing_rate": round((missing_points / max(total_channels, 1)) * 100, 2),
        "partition": "validation",
    }


def load_evaluation(partition: str) -> dict:
    if partition not in {"validation", "test"}:
        raise ValueError("partition must be 'validation' or 'test'")
    path = ROOT / "artifacts" / "evaluation" / f"{partition}_evaluation.json"
    if not path.exists():
        raise FileNotFoundError(f"No {partition} evaluation report exists at {path}.")
    return json.loads(path.read_text())


def evaluation_series(
    partition: str,
    sensor: str,
    cycle_id: int | None = None,
    include_reference: bool = False,
) -> dict:
    if partition not in {"validation", "test"}:
        raise ValueError("partition must be 'validation' or 'test'")
    if sensor not in SENSORS:
        raise ValueError(f"Unknown sensor: {sensor}")
    partition_data = load_partition_data(partition)
    cycles = sorted(int(value) for value in partition_data["cycle_id"].unique())
    if cycle_id is None:
        labels_path = DATA_DIR / "corrupted" / f"{partition}_labels_private.parquet"
        labels = pd.read_parquet(labels_path)
        sensor_labels = labels[labels["sensor_id"] == sensor]
        cycle_counts = sensor_labels.groupby("cycle_id").size()
        cycle_id = int(cycle_counts.sort_values(ascending=False, kind="stable").index[0]) if not cycle_counts.empty else cycles[0]
    if cycle_id not in cycles:
        raise ValueError(f"Unknown cycle_id {cycle_id} for the {partition} partition.")

    corrupted = partition_data[partition_data["cycle_id"] == cycle_id].copy()
    corrupted = corrupted.sort_values("second_in_cycle")
    results = detect_faults(corrupted, load_model_map())
    results = apply_calibrated_confidence(results)
    sensor_results = results[results["sensor_id"] == sensor].sort_values(
        ["cycle_id", "second_in_cycle"]
    )
    if include_reference:
        reference = pd.read_parquet(DATA_DIR / "splits" / f"{partition}_reference.parquet")
        ref_points = reference[reference["cycle_id"] == cycle_id][
            ["cycle_id", "second_in_cycle", sensor]
        ].rename(columns={sensor: "reference_value"})
        sensor_results = sensor_results.merge(
            ref_points,
            on=["cycle_id", "second_in_cycle"],
            how="left",
            validate="one_to_one",
        )
    points = _json_records(sensor_results)
    series = {
        "sensor": sensor,
        "partition": partition,
        "selected_cycle": cycle_id,
        "cycles": cycles,
        "x": list(range(len(points))),
        "observed": [point["observed_value"] for point in points],
        "repaired": [point["reconstructed_value"] for point in points],
        "faults": [point["fault_type"] for point in points],
        "statuses": [point["status"] for point in points],
        "confidence": [point["confidence"] for point in points],
        "points": points,
    }
    if include_reference:
        series["reference"] = [point["reference_value"] for point in points]
    return series


def select_run_series_cycle(series: dict, cycle_id: int | None = None) -> dict:
    points = series.get("points", [])
    if not points:
        return series
    cycles = sorted({int(point["cycle_id"]) for point in points if point.get("cycle_id") is not None})
    if not cycles:
        return series
    if cycle_id is None:
        fault_counts: dict[int, int] = {}
        for point in points:
            current_cycle = point.get("cycle_id")
            if current_cycle is not None and point.get("fault_type") != "healthy":
                current_cycle = int(current_cycle)
                fault_counts[current_cycle] = fault_counts.get(current_cycle, 0) + 1
        cycle_id = min(cycles, key=lambda value: (-fault_counts.get(value, 0), value))
    if cycle_id not in cycles:
        raise ValueError(f"Unknown cycle_id {cycle_id} for this run.")
    selected = [
        point for point in points
        if point.get("cycle_id") is not None and int(point["cycle_id"]) == cycle_id
    ]
    selected.sort(key=lambda point: int(point["second_in_cycle"]))
    return {
        "sensor": series.get("sensor"),
        "cycles": cycles,
        "selected_cycle": cycle_id,
        "x": list(range(len(selected))),
        "observed": [point.get("observed_value") for point in selected],
        "repaired": [point.get("reconstructed_value") for point in selected],
        "faults": [point.get("fault_type") for point in selected],
        "statuses": [point.get("status") for point in selected],
        "confidence": [point.get("confidence") for point in selected],
        "points": selected,
    }


def generate_run(name: str, partition: str, frame: pd.DataFrame, owner_id: int | None = None) -> dict:
    ensure_models()
    models = load_model_map()
    out = detect_faults(frame, models)
    out = apply_calibrated_confidence(out)
    metrics = calibrate_run(out)
    run = {
        "run_id": 0,
        "name": name,
        "partition": partition,
        "status": "completed",
        "metrics": metrics,
        "events": _json_records(
            out[out["modified"]].sort_values(
                ["fault_type", "cycle_id", "second_in_cycle"]
            )
        ),
        "series": {
            sensor: _series_from_results(out, sensor)
            for sensor in SENSORS
        },
    }
    return run


def fetch_run_list() -> list[dict]:
    return list_runs()


def create_run_record(
    name: str,
    partition: str = "validation",
    owner_id: int | None = None,
) -> dict:
    run = generate_run(name, partition, load_partition_data(partition), owner_id)
    run_id = insert_run(
        name,
        partition,
        run["status"],
        run["metrics"],
        run["events"],
        run["series"],
        owner_id,
    )
    run["run_id"] = run_id
    return run


def create_upload_run(name: str, frame: pd.DataFrame, owner_id: int) -> dict:
    run = generate_run(name, "upload", frame, owner_id)
    run_id = insert_run(
        name,
        "upload",
        run["status"],
        run["metrics"],
        run["events"],
        run["series"],
        owner_id,
    )
    run["run_id"] = run_id
    return run


def sample_chart_points() -> dict:
    results = detect_faults(
        load_partition_data("validation").head(120),
        load_model_map(),
    )
    return {
        sensor: _series_from_results(results, sensor)
        for sensor in SENSORS
    }


def _series_from_results(results: pd.DataFrame, sensor: str) -> dict:
    series = results[results["sensor_id"] == sensor].sort_values(
        ["cycle_id", "second_in_cycle"]
    )
    return {
        "sensor": sensor,
        "x": list(range(len(series))),
        "observed": [
            float(value) if pd.notna(value) else None
            for value in series["observed_value"]
        ],
        "repaired": [
            float(value) if pd.notna(value) else None
            for value in series["reconstructed_value"]
        ],
        "faults": series["fault_type"].tolist(),
        "confidence": [float(value) for value in series["confidence"]],
        "statuses": series["status"].tolist(),
        "points": _json_records(series),
    }
