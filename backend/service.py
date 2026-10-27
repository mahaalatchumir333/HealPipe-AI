from __future__ import annotations

import json
from pathlib import Path

import numpy as np
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


def evaluate_scenario(
    partition: str,
    sensor: str,
    cycle_id: int | None,
    fault_type: str,
    start_second: int,
    duration_seconds: int,
    severity: float,
    affected_channels: int,
) -> dict:
    if partition not in {"validation", "test"}:
        raise ValueError("partition must be 'validation' or 'test'")
    if sensor not in SENSORS:
        raise ValueError(f"Unknown sensor: {sensor}")
    if fault_type not in {"missing", "freeze", "spike", "drift"}:
        raise ValueError(f"Unknown fault type: {fault_type}")
    if start_second < 1 or start_second >= 60:
        raise ValueError("start_second must be between 1 and 59.")
    if duration_seconds < 1 or duration_seconds > 30:
        raise ValueError("duration_seconds must be between 1 and 30.")
    if affected_channels < 1 or affected_channels > 3:
        raise ValueError("affected_channels must be between 1 and 3.")
    actual_duration = 1 if fault_type == "spike" else duration_seconds
    if start_second + actual_duration > 60:
        raise ValueError("The fault duration must fit within the selected 60-second cycle.")
    if fault_type == "freeze" and not 3 <= duration_seconds <= 10:
        raise ValueError("Freeze duration must be between 3 and 10 seconds.")
    if fault_type == "drift" and not 10 <= duration_seconds <= 30:
        raise ValueError("Drift duration must be between 10 and 30 seconds.")
    if fault_type in {"spike", "drift"} and not 3 <= severity <= 12:
        raise ValueError("Spike and drift severity must be between 3 and 12 sensor scales.")

    reference = pd.read_parquet(DATA_DIR / "splits" / f"{partition}_reference.parquet")
    cycles = sorted(int(value) for value in reference["cycle_id"].unique())
    if not cycles:
        raise ValueError(f"The {partition} partition contains no complete cycles.")
    if cycle_id is None:
        cycle_id = cycles[0]
    if cycle_id not in cycles:
        raise ValueError(f"Unknown cycle_id {cycle_id} for the {partition} partition.")

    clean_cycle = reference[reference["cycle_id"] == cycle_id].sort_values(
        "second_in_cycle"
    ).reset_index(drop=True)
    if len(clean_cycle) != 60 or clean_cycle["second_in_cycle"].tolist() != list(range(60)):
        raise ValueError(f"Cycle {cycle_id} does not contain a complete 60-second grid.")

    sensor_index = SENSORS.index(sensor)
    target_sensors = [
        SENSORS[(sensor_index + offset) % len(SENSORS)]
        for offset in range(affected_channels)
    ]
    scales_path = ROOT / "artifacts" / "reference_scales.json"
    scales = json.loads(scales_path.read_text())
    corrupted = clean_cycle[["cycle_id", "second_in_cycle", *SENSORS]].copy()
    end_second = start_second + actual_duration

    for target_sensor in target_sensors:
        clean_values = clean_cycle[target_sensor].to_numpy(dtype=float)
        values = clean_values.copy()
        scale = max(float(scales.get(target_sensor, 1.0)), 1e-8)
        if fault_type == "missing":
            values[start_second:end_second] = np.nan
        elif fault_type == "freeze":
            values[start_second:end_second] = values[start_second - 1]
        elif fault_type == "spike":
            values[start_second] += severity * scale
        else:
            ramp = np.linspace(0.0, severity * scale, actual_duration + 1)[1:]
            values[start_second:end_second] += ramp
        corrupted[target_sensor] = values

    results = apply_calibrated_confidence(
        detect_faults(corrupted, load_model_map())
    )
    target_mask = (
        results["sensor_id"].isin(target_sensors)
        & results["second_in_cycle"].between(start_second, end_second - 1)
    )
    target_results = results[target_mask]
    correct_diagnoses = target_results["fault_type"].eq(fault_type)
    any_diagnoses = target_results["fault_type"].ne("healthy")
    accepted = target_results["acceptance_decision"].eq("accepted")
    refused = target_results["acceptance_decision"].eq("refused")
    tolerance = target_results["sensor_id"].map(
        lambda target_sensor: max(float(scales.get(target_sensor, 1.0)), 1e-8) * 1.5
    )
    accepted_success = pd.Series(False, index=target_results.index)
    reference_by_second = clean_cycle.set_index("second_in_cycle")
    for target_sensor in target_sensors:
        sensor_mask = target_results["sensor_id"].eq(target_sensor)
        seconds = target_results.loc[sensor_mask, "second_in_cycle"].to_numpy(dtype=int)
        reference_values = reference_by_second.loc[seconds, target_sensor].to_numpy(dtype=float)
        accepted_success.loc[sensor_mask] = np.abs(
            target_results.loc[sensor_mask, "reconstructed_value"].to_numpy(dtype=float)
            - reference_values
        ) <= tolerance.loc[sensor_mask].to_numpy(dtype=float)
    accepted_success &= accepted
    predicted_faults = results["fault_type"].ne("healthy")
    false_alarms = int((~target_mask & predicted_faults).sum())
    true_positive_count = int(correct_diagnoses.sum())
    predicted_class_count = int(results["fault_type"].eq(fault_type).sum())
    total_fault_points = int(target_mask.sum())
    precision = true_positive_count / predicted_class_count if predicted_class_count else 0.0
    recall = true_positive_count / total_fault_points if total_fault_points else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    selected_results = results[results["sensor_id"] == sensor].sort_values(
        ["cycle_id", "second_in_cycle"]
    )
    points = _json_records(selected_results)
    return {
        "scenario": {
            "partition": partition,
            "cycle_id": int(cycle_id),
            "fault_type": fault_type,
            "start_second": start_second,
            "duration_seconds": actual_duration,
            "severity_sensor_scales": severity if fault_type in {"spike", "drift"} else None,
            "sensors": target_sensors,
            "deterministic": True,
            "reference_values_sent_to_detector": False,
        },
        "metrics": {
            "injected_points": total_fault_points,
            "diagnosed_fault_points": int(any_diagnoses.sum()),
            "correctly_diagnosed_points": true_positive_count,
            "diagnosis_precision": round(precision * 100, 2),
            "diagnosis_recall": round(recall * 100, 2),
            "diagnosis_f1": round(f1 * 100, 2),
            "accepted_repairs": int(accepted.sum()),
            "refused_repairs": int(refused.sum()),
            "mean_accepted_confidence": (
                round(float(target_results.loc[accepted, "confidence"].mean()), 2)
                if accepted.any() else None
            ),
            "accepted_repairs_within_tolerance": int(accepted_success.sum()),
            "accepted_repair_success_rate": (
                round(float(accepted_success.sum() / accepted.sum() * 100), 2)
                if accepted.any() else None
            ),
            "false_alarms_outside_injected_region": false_alarms,
            "status_counts": {
                str(key): int(value)
                for key, value in results["status"].value_counts().items()
            },
            "tolerance_definition": "Accepted repair error <= 1.5x robust training reference scale.",
        },
        "fault_audit": _json_records(target_results),
        "series": {
            "sensor": sensor,
            "partition": partition,
            "selected_cycle": int(cycle_id),
            "x": list(range(len(points))),
            "observed": [point["observed_value"] for point in points],
            "repaired": [point["reconstructed_value"] for point in points],
            "faults": [point["fault_type"] for point in points],
            "statuses": [point["status"] for point in points],
            "confidence": [point["confidence"] for point in points],
            "points": points,
        },
    }


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
