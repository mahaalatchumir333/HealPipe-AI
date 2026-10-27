from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, confusion_matrix

from pipeline.calibrate import ARTIFACT as CONFIDENCE_ARTIFACT, apply_calibrated_confidence
from pipeline.detect import detect_faults
from pipeline.reconstruct import SENSORS, ensure_models, load_model_map

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "Data"
RESULTS = ROOT / "artifacts" / "evaluation"


def evaluate(partition: str = "test") -> dict:
    started_at = time.perf_counter()
    if partition not in {"validation", "test", "test_v2"}:
        raise ValueError("partition must be 'validation', 'test', or 'test_v2'")

    ensure_models()
    source_partition = "test" if partition == "test_v2" else partition
    corrupted = pd.read_parquet(DATA / "corrupted" / f"{partition}_corrupted.parquet")
    reference = pd.read_parquet(DATA / "splits" / f"{source_partition}_reference.parquet")
    labels_path = DATA / "corrupted" / f"{partition}_labels_private.parquet"
    labels = pd.read_parquet(labels_path)
    detections = detect_faults(corrupted, load_model_map())
    if CONFIDENCE_ARTIFACT.exists():
        detections = apply_calibrated_confidence(detections)
    else:
        raise FileNotFoundError(
            f"Confidence calibration is missing at {CONFIDENCE_ARTIFACT}. "
            "Run `python -m pipeline.calibrate` first."
        )

    expected = reference.melt(
        id_vars=["cycle_id", "second_in_cycle"],
        value_vars=SENSORS,
        var_name="sensor_id",
        value_name="reference_value",
    )
    expected["truth"] = "healthy"
    expected = expected.merge(
        labels[["cycle_id", "second_in_cycle", "sensor_id", "injected_fault"]],
        on=["cycle_id", "second_in_cycle", "sensor_id"],
        how="left",
    )
    expected["truth"] = expected["injected_fault"].fillna(expected["truth"])
    results = detections.merge(
        expected[["cycle_id", "second_in_cycle", "sensor_id", "reference_value", "truth"]],
        on=["cycle_id", "second_in_cycle", "sensor_id"],
        how="left",
        validate="one_to_one",
    )

    class_names = ["healthy", "missing", "freeze", "spike", "drift"]
    report = classification_report(
        results["truth"],
        results["fault_type"],
        labels=class_names,
        output_dict=True,
        zero_division=0,
    )
    matrix = confusion_matrix(
        results["truth"],
        results["fault_type"],
        labels=class_names,
    )
    modified = results[results["modified"]]
    repaired = modified["reconstructed_value"].to_numpy(dtype=float)
    references = modified["reference_value"].to_numpy(dtype=float)
    mae = float(np.mean(np.abs(repaired - references))) if len(modified) else None
    rmse = float(np.sqrt(np.mean(np.square(repaired - references)))) if len(modified) else None
    accuracy_by_sensor = {}
    for sensor, sensor_rows in modified.groupby("sensor_id"):
        errors = sensor_rows["reconstructed_value"] - sensor_rows["reference_value"]
        accuracy_by_sensor[sensor] = {
            "count": int(len(sensor_rows)),
            "mae": float(np.mean(np.abs(errors))),
            "rmse": float(np.sqrt(np.mean(np.square(errors)))),
        }

    fault_points = results[results["truth"] != "healthy"].copy()
    baselines = _baseline_metrics(fault_points, detections, corrupted)
    status_counts = results["status"].value_counts()
    reliability = _reliability_bins(fault_points)
    accepted_faults = fault_points[fault_points["acceptance_decision"] == "accepted"]
    refused_faults = fault_points[fault_points["acceptance_decision"] == "refused"]
    scales_path = ROOT / "artifacts" / "reference_scales.json"
    scales = json.loads(scales_path.read_text())
    accepted_tolerance = accepted_faults["sensor_id"].map(
        lambda sensor: max(float(scales.get(sensor, 1.0)), 1e-8) * 1.5
    )
    accepted_success_count = int(
        (
            (accepted_faults["reconstructed_value"] - accepted_faults["reference_value"]).abs()
            <= accepted_tolerance
        ).sum()
    )
    event_sizes = labels.groupby("event_id")["sensor_id"].nunique() if "event_id" in labels else pd.Series(dtype=int)
    modified_confidence = results.loc[results["modified"], "confidence"]
    untouched_cycle_ids = set(reference["cycle_id"].unique()) - set(labels["cycle_id"].unique())
    false_alarm_cycles = set(
        results.loc[
            results["cycle_id"].isin(untouched_cycle_ids)
            & (results["fault_type"] != "healthy"),
            "cycle_id",
        ].unique()
    )
    healthy_points = results[results["truth"] == "healthy"]
    unintended_diagnoses = healthy_points[healthy_points["fault_type"] != "healthy"]
    unintended_changes = healthy_points[healthy_points["modified"]]
    result = {
        "partition": partition,
        "source_partition": source_partition,
        "cycles": int(reference["cycle_id"].nunique()),
        "telemetry_seconds": int(len(reference)),
        "duration_seconds": round(time.perf_counter() - started_at, 3),
        "points": int(len(results)),
        "injected_points": int(len(labels)),
        "detected_points": int((results["fault_type"] != "healthy").sum()),
        "modified_points": int(len(modified)),
        "mean_modified_confidence": (
            round(float(modified_confidence.mean()), 2) if not modified_confidence.empty else None
        ),
        "repair_mae": mae,
        "repair_rmse": rmse,
        "status_counts": {str(key): int(value) for key, value in status_counts.items()},
        "decision_metrics": {
            "accepted_repairs": int(len(accepted_faults)),
            "refused_repairs": int(len(refused_faults)),
            "accepted_repair_success_rate": (
                round(accepted_success_count / len(accepted_faults) * 100, 2)
                if len(accepted_faults) else None
            ),
            "fault_decision_coverage": (
                round(len(accepted_faults) / len(fault_points) * 100, 2)
                if len(fault_points) else 0.0
            ),
            "decision_scope": "Injected fault coordinates; success means error within 1.5x sensor reference scale.",
        },
        "false_positives": {
            "untouched_cycles": int(len(untouched_cycle_ids)),
            "untouched_cycles_with_any_diagnosis": int(len(false_alarm_cycles)),
            "untouched_cycle_false_positive_rate": (
                len(false_alarm_cycles) / len(untouched_cycle_ids)
                if untouched_cycle_ids else None
            ),
            "healthy_point_false_positive_rate": (
                len(unintended_diagnoses) / len(healthy_points)
                if len(healthy_points) else None
            ),
        },
        "unintended_changes": {
            "healthy_points_diagnosed_as_fault": int(len(unintended_diagnoses)),
            "healthy_points_modified": int(len(unintended_changes)),
            "scope": "Points without injected faults; includes only detector diagnoses and repairs.",
        },
        "unresolved_points": int((results["status"] == "Unresolved").sum()),
        "fault_distribution": {
            str(key): int(value)
            for key, value in results.loc[results["fault_type"] != "healthy", "fault_type"].value_counts().items()
        },
        "class_names": class_names,
        "confusion_matrix": matrix.tolist(),
        "classification_report": report,
        "repair_mae_by_sensor": accuracy_by_sensor,
        "repair_baselines": baselines,
        "confidence_reliability": reliability,
        "multi_sensor_events": {
            "single_channel": int((event_sizes == 1).sum()),
            "two_channels": int((event_sizes == 2).sum()),
            "three_or_more_channels": int((event_sizes >= 3).sum()),
            "available": bool(len(event_sizes)),
        },
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    report_name = "test_evaluation_v2.json" if partition == "test_v2" else f"{partition}_evaluation.json"
    (RESULTS / report_name).write_text(json.dumps(result, indent=2))
    return result


def _baseline_metrics(
    fault_points: pd.DataFrame,
    detections: pd.DataFrame,
    corrupted: pd.DataFrame,
) -> dict:
    if fault_points.empty:
        return {}

    keys = ["cycle_id", "second_in_cycle", "sensor_id"]
    detector_fields = ["observed_value", "prediction", "reconstructed_value", "fault_type", "status"]
    missing_fields = [field for field in detector_fields if field not in fault_points.columns]
    candidates = fault_points.merge(
        detections[keys + missing_fields],
        on=keys,
        how="left",
        validate="one_to_one",
    ).sort_values(keys)
    predictions: dict[str, dict[tuple[int, int, str], float]] = {
        "forward_fill": {},
        "linear_interpolation": {},
        "cross_sensor_model": {},
    }

    for sensor in SENSORS:
        sensor_faults = candidates[candidates["sensor_id"] == sensor]
        for cycle_id, target_rows in sensor_faults.groupby("cycle_id", sort=False):
            cycle = corrupted[corrupted["cycle_id"] == cycle_id].sort_values(
                "second_in_cycle"
            ).reset_index(drop=True)
            positions = target_rows["second_in_cycle"].to_numpy(dtype=int)
            observed = cycle[sensor].astype(float).copy()
            masked = observed.copy()
            masked.iloc[positions] = np.nan
            forward_fill = masked.ffill().bfill()
            interpolation = masked.interpolate(limit_direction="both")
            fallback_by_second = target_rows.set_index("second_in_cycle")["prediction"]
            for position in positions:
                key = (int(cycle_id), int(position), sensor)
                predictions["forward_fill"][key] = (
                    float(forward_fill.iloc[position])
                    if pd.notna(forward_fill.iloc[position])
                    else float(fallback_by_second.loc[position])
                )
                predictions["linear_interpolation"][key] = (
                    float(interpolation.iloc[position])
                    if pd.notna(interpolation.iloc[position])
                    else float(fallback_by_second.loc[position])
                )
                predictions["cross_sensor_model"][key] = (
                    float(fallback_by_second.loc[position])
                )

    reference = candidates["reference_value"].to_numpy(dtype=float)
    candidate_keys = [
        (int(row.cycle_id), int(row.second_in_cycle), row.sensor_id)
        for row in candidates.itertuples()
    ]
    output = {}
    for method, values in predictions.items():
        estimates = np.asarray([values[key] for key in candidate_keys], dtype=float)
        errors = estimates - reference
        output[method] = {
            "points": int(len(errors)),
            "mae": float(np.mean(np.abs(errors))),
            "rmse": float(np.sqrt(np.mean(np.square(errors)))),
            "scope": "Injected points only; oracle fault coordinates are masked for baseline repair.",
        }
    return output


def _reliability_bins(fault_points: pd.DataFrame, bins: int = 10) -> list[dict]:
    scored = fault_points[fault_points["modified"]].copy()
    if scored.empty:
        return []
    scales_path = ROOT / "artifacts" / "reference_scales.json"
    scales = json.loads(scales_path.read_text())
    tolerance = scored["sensor_id"].map(
        lambda sensor: max(float(scales.get(sensor, 1.0)), 1e-8) * 1.5
    )
    scored["success"] = (
        (scored["reconstructed_value"] - scored["reference_value"]).abs() <= tolerance
    ).astype(float)
    scored["confidence_bin"] = np.minimum(
        (scored["confidence"].clip(0, 100) / 100 * bins).astype(int),
        bins - 1,
    )
    rows = []
    for bin_index, group in scored.groupby("confidence_bin", sort=True):
        rows.append({
            "bin": int(bin_index),
            "count": int(len(group)),
            "mean_confidence": float(group["confidence"].mean()),
            "observed_success_rate": float(group["success"].mean() * 100),
        })
    return rows


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Evaluate a fixed dataset partition once.")
    parser.add_argument("--partition", choices=["validation", "test", "test_v2"], default="test")
    args = parser.parse_args()
    output = evaluate(args.partition)
    print(json.dumps({
        key: output[key]
        for key in ("partition", "points", "injected_points", "detected_points", "modified_points", "repair_mae")
    }, indent=2))
