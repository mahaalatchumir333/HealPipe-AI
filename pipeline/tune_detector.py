from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline import detect
from pipeline.reconstruct import SENSORS, ensure_models, load_model_map

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "Data"
PARAMS_PATH = ROOT / "artifacts" / "detector_params.json"
FAULTS = ("missing", "freeze", "spike", "drift")
DEFAULTS = {
    "freeze_windows": [3, 5, 10],
    "freeze_tolerance_scale": 0.015,
    "freeze_motion_scale": 0.75,
    "freeze_disagreement_scale": 1.5,
    "spike_score_threshold": 6.0,
    "spike_residual_scale": 3.0,
    "drift_window": 10,
    "drift_slope_scale": 0.12,
    "drift_change_scale": 0.75,
    "drift_residual_scale": 1.0,
    "drift_r_squared": 0.85,
}


def _validation_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    corrupted = pd.read_parquet(DATA / "corrupted" / "validation_corrupted.parquet")
    labels = pd.read_parquet(DATA / "corrupted" / "validation_labels_private.parquet")
    return corrupted, labels


def _truth_frame(labels: pd.DataFrame, corrupted: pd.DataFrame) -> pd.DataFrame:
    keys = ["cycle_id", "second_in_cycle", "sensor_id"]
    all_points = corrupted.melt(
        id_vars=["cycle_id", "second_in_cycle"],
        value_vars=SENSORS,
        var_name="sensor_id",
        value_name="observed_value",
    )
    truth = all_points.merge(
        labels[keys + ["injected_fault", "severity", "event_id"]],
        on=keys,
        how="left",
        validate="one_to_one",
    )
    truth["truth"] = truth["injected_fault"].fillna("healthy")
    return truth


def _metrics(frame: pd.DataFrame) -> dict:
    result: dict[str, dict[str, float | int]] = {}
    for fault in FAULTS:
        true_positive = int(((frame["truth"] == fault) & (frame["fault_type"] == fault)).sum())
        predicted = int((frame["fault_type"] == fault).sum())
        actual = int((frame["truth"] == fault).sum())
        precision = true_positive / predicted if predicted else 0.0
        recall = true_positive / actual if actual else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        result[fault] = {
            "support": actual,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    clean_cycles = set(frame.loc[frame["truth"] == "healthy", "cycle_id"])
    cycles_with_faults = set(frame.loc[frame["truth"] != "healthy", "cycle_id"])
    untouched_cycles = clean_cycles - cycles_with_faults
    predicted_fault_cycles = set(
        frame.loc[
            frame["cycle_id"].isin(untouched_cycles) & (frame["fault_type"] != "healthy"),
            "cycle_id",
        ]
    )
    return {
        "per_fault": result,
        "untouched_cycles": {
            "count": len(untouched_cycles),
            "cycles_with_any_false_alarm": len(predicted_fault_cycles),
            "false_positive_cycle_rate": (
                len(predicted_fault_cycles) / len(untouched_cycles) if untouched_cycles else None
            ),
        },
        "point_false_positive_rate": (
            int(((frame["truth"] == "healthy") & (frame["fault_type"] != "healthy")).sum())
            / max(int((frame["truth"] == "healthy").sum()), 1)
        ),
    }


def _severity_parts(value: object, fault: str) -> tuple[int, float | None]:
    text = str(value or "")
    length = re.search(r"len=(\d+)", text)
    magnitude = re.search(r"k=([0-9.]+)", text)
    duration = int(length.group(1)) if length else (1 if fault == "spike" else 0)
    severity = float(magnitude.group(1)) if magnitude else None
    return duration, severity


def _stratified_metrics(frame: pd.DataFrame) -> dict:
    labels = frame[frame["truth"] != "healthy"].copy()
    if labels.empty:
        return {"by_fault_duration_severity": [], "inherently_undetectable": []}
    labels[["duration_seconds", "severity_value"]] = labels.apply(
        lambda row: pd.Series(_severity_parts(row["severity"], row["truth"])),
        axis=1,
    )
    groups = []
    for (fault, duration, severity), subset in labels.groupby(
        ["truth", "duration_seconds", "severity_value"], dropna=False, sort=True
    ):
        tp = int(((subset["truth"] == subset["fault_type"])).sum())
        actual = len(subset)
        predicted = int((subset["fault_type"] == fault).sum())
        precision = tp / predicted if predicted else 0.0
        recall = tp / actual if actual else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        groups.append({
            "fault_type": str(fault),
            "duration_seconds": int(duration),
            "severity_sensor_scales": float(severity) if pd.notna(severity) else None,
            "support": actual,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        })
    one_second_freeze = int(((labels["truth"] == "freeze") & (labels["duration_seconds"] == 1)).sum())
    return {
        "by_fault_duration_severity": groups,
        "inherently_undetectable": [{
            "case": "one-second freeze",
            "validation_examples": one_second_freeze,
            "reason": (
                "A single frozen sample has no within-run repetition; it cannot be distinguished "
                "from a legitimate measurement without independent evidence. This case is reported, "
                "not tuned."
            ),
        }],
    }


def _evaluate_params(
    corrupted: pd.DataFrame,
    labels: pd.DataFrame,
    models: dict[str, object],
    params: dict,
) -> tuple[pd.DataFrame, dict]:
    with tempfile.TemporaryDirectory(prefix="healpipe-detector-") as directory:
        config_path = Path(directory) / "detector_params.json"
        config_path.write_text(
            json.dumps({"schema_version": 1, "parameters": params}),
            encoding="utf-8",
        )
        original_path = detect.PARAMS_PATH
        try:
            detect.PARAMS_PATH = config_path
            detections = detect.detect_faults(corrupted, models)
        finally:
            detect.PARAMS_PATH = original_path
    truth = _truth_frame(labels, corrupted)
    merged = detections.merge(
        truth[[
            "cycle_id", "second_in_cycle", "sensor_id", "truth",
            "severity", "event_id",
        ]],
        on=["cycle_id", "second_in_cycle", "sensor_id"],
        how="left",
        validate="one_to_one",
    )
    return merged, _metrics(merged)


def _candidate_grid() -> list[dict]:
    candidates = []
    for freeze_motion, freeze_disagreement, drift_slope, drift_change in (
        (0.75, 1.5, 0.12, 0.75),
        (0.5, 1.0, 0.08, 0.6),
        (1.0, 2.0, 0.16, 0.9),
        (0.75, 1.5, 0.08, 0.9),
    ):
        candidates.append({
            **DEFAULTS,
            "freeze_motion_scale": freeze_motion,
            "freeze_disagreement_scale": freeze_disagreement,
            "drift_slope_scale": drift_slope,
            "drift_change_scale": drift_change,
        })
    return candidates


def tune_detector(search_cycles: int = 80, seed: int = 1701) -> dict:
    ensure_models()
    corrupted, labels = _validation_frames()
    cycle_ids = np.sort(corrupted["cycle_id"].unique())
    rng = np.random.default_rng(seed)
    selected_count = min(max(1, search_cycles), len(cycle_ids))
    selected_cycles = set(rng.choice(cycle_ids, size=selected_count, replace=False).tolist())
    search_corrupted = corrupted[corrupted["cycle_id"].isin(selected_cycles)]
    search_labels = labels[labels["cycle_id"].isin(selected_cycles)]
    models = load_model_map()

    candidate_scores = []
    for index, parameters in enumerate(_candidate_grid()):
        _, scores = _evaluate_params(search_corrupted, search_labels, models, parameters)
        weak_fault_scores = [
            scores["per_fault"][fault]["f1"]
            for fault in ("freeze", "drift")
            if scores["per_fault"][fault]["support"]
        ]
        objective = float(np.mean(weak_fault_scores)) if weak_fault_scores else 0.0
        candidate_scores.append({
            "candidate": index,
            "parameters": parameters,
            "objective_freeze_drift_macro_f1": objective,
            "search_scores": scores,
        })
    chosen = max(candidate_scores, key=lambda item: (item["objective_freeze_drift_macro_f1"], -item["candidate"]))
    full_validation, validation_metrics = _evaluate_params(
        corrupted,
        labels,
        models,
        chosen["parameters"],
    )
    report = {
        "report_version": "validation-detector-tuning-v1",
        "partition": "validation",
        "seed": seed,
        "search_cycles": selected_count,
        "validation_cycles": int(len(cycle_ids)),
        "objective": "Macro F1 over freeze and drift point labels on selected validation cycles.",
        "candidates": candidate_scores,
        "chosen_candidate": chosen["candidate"],
        "parameters": chosen["parameters"],
        "validation_scores": {
            **validation_metrics,
            **_stratified_metrics(full_validation),
        },
        "warning": (
            "Validation estimates are for tuning only. The held-out test set was not read "
            "or used by this command."
        ),
    }
    PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PARAMS_PATH.write_text(json.dumps({
        "schema_version": 1,
        "version": "2026-10-validation-1",
        "tuning_partition": "validation",
        "parameters": chosen["parameters"],
        "validation_scores": report["validation_scores"],
    }, indent=2), encoding="utf-8")
    report_path = ROOT / "artifacts" / "evaluation" / "validation_detector_tuning.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Tune detector parameters using validation data only.")
    parser.add_argument("--search-cycles", type=int, default=80)
    parser.add_argument("--seed", type=int, default=1701)
    args = parser.parse_args()
    print(json.dumps(tune_detector(args.search_cycles, args.seed), indent=2))


if __name__ == "__main__":
    main()
