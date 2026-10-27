from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from sklearn.model_selection import GroupKFold, cross_val_predict
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from pipeline.detect import detect_faults
from pipeline.reconstruct import SENSORS, ensure_models, load_model_map

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "Data"
ARTIFACT = ROOT / "artifacts" / "calibration" / "confidence.joblib"
REPORT = ROOT / "artifacts" / "calibration" / "confidence_report.json"
CONFIDENCE_THRESHOLD = 50.0
FEATURES = [
    "gap_length",
    "healthy_support",
    "prediction_disagreement",
    "evidence_strength",
    "heuristic_confidence",
    "fault_missing",
    "fault_freeze",
    "fault_spike",
    "fault_drift",
]


def _feature_frame(results: pd.DataFrame) -> pd.DataFrame:
    frame = pd.DataFrame(index=results.index)
    frame["gap_length"] = np.log1p(results["gap_length"].astype(float))
    frame["healthy_support"] = results["healthy_support"].astype(float)
    frame["prediction_disagreement"] = np.log1p(
        results["prediction_disagreement"].astype(float).clip(lower=0)
    )
    frame["evidence_strength"] = np.log1p(
        results["evidence_strength"].astype(float).clip(lower=0)
    )
    frame["heuristic_confidence"] = results["confidence"].astype(float) / 100.0
    for fault in ("missing", "freeze", "spike", "drift"):
        frame[f"fault_{fault}"] = results["fault_type"].eq(fault).astype(float)
    return frame[FEATURES].replace([np.inf, -np.inf], np.nan).fillna(0.0)


def train_confidence_model() -> dict:
    ensure_models()
    corrupted = pd.read_parquet(DATA / "corrupted" / "validation_corrupted.parquet")
    reference = pd.read_parquet(DATA / "splits" / "validation_reference.parquet")
    labels = pd.read_parquet(DATA / "corrupted" / "validation_labels_private.parquet")
    detected = detect_faults(corrupted, load_model_map())
    target = reference.melt(
        id_vars=["cycle_id", "second_in_cycle"],
        value_vars=SENSORS,
        var_name="sensor_id",
        value_name="reference_value",
    )
    samples = detected.merge(
        labels[["cycle_id", "second_in_cycle", "sensor_id"]],
        on=["cycle_id", "second_in_cycle", "sensor_id"],
        how="inner",
        validate="one_to_one",
    ).merge(
        target,
        on=["cycle_id", "second_in_cycle", "sensor_id"],
        how="left",
        validate="one_to_one",
    )
    samples = samples[samples["modified"]].copy()
    if samples.empty:
        raise RuntimeError("No injected validation readings were repaired; confidence model cannot be trained.")

    scales_path = ROOT / "artifacts" / "reference_scales.json"
    scales = json.loads(scales_path.read_text())
    tolerance = samples["sensor_id"].map(
        lambda sensor: max(float(scales.get(sensor, 1.0)), 1e-8) * 1.5
    )
    samples["success"] = (
        (samples["reconstructed_value"] - samples["reference_value"]).abs() <= tolerance
    ).astype(int)
    if samples["success"].nunique() != 2:
        raise RuntimeError("Calibration samples must include both successful and unsuccessful repairs.")

    X = _feature_frame(samples)
    y = samples["success"].to_numpy()
    groups = samples["cycle_id"].to_numpy()
    n_splits = min(5, len(np.unique(groups)))
    if n_splits < 2:
        raise RuntimeError("At least two validation cycles are required for grouped calibration.")

    pipeline = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42),
    )
    splitter = GroupKFold(n_splits=n_splits)
    out_of_fold = cross_val_predict(
        pipeline,
        X,
        y,
        groups=groups,
        cv=splitter,
        method="predict_proba",
    )[:, 1]
    pipeline.fit(X, y)
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, ARTIFACT)
    report = {
        "method": "LogisticRegression with StandardScaler; grouped out-of-fold Brier score",
        "samples": int(len(samples)),
        "successes": int(y.sum()),
        "failures": int(len(y) - y.sum()),
        "folds": int(n_splits),
        "tolerance": "1.5 x robust reference scale per sensor",
        "out_of_fold_brier_score": float(brier_score_loss(y, out_of_fold)),
        "artifact": str(ARTIFACT.relative_to(ROOT)),
    }
    REPORT.write_text(json.dumps(report, indent=2))
    return report


def apply_calibrated_confidence(results: pd.DataFrame) -> pd.DataFrame:
    results = results.copy()
    if not ARTIFACT.exists():
        raise FileNotFoundError(
            f"Missing calibrated confidence model at {ARTIFACT}. "
            "Run `python -m pipeline.calibrate` before production scoring."
        )
    model = joblib.load(ARTIFACT)
    probabilities = model.predict_proba(_feature_frame(results))[:, 1] * 100.0
    results["confidence"] = np.round(probabilities, 2)
    results["confidence_method"] = "validation_calibrated_logistic_regression"
    results["confidence_threshold"] = CONFIDENCE_THRESHOLD
    modified = results["fault_type"] != "healthy"
    unresolved = modified & (results["confidence"] < CONFIDENCE_THRESHOLD)
    resolved = modified & ~unresolved
    results.loc[unresolved, "status"] = "Unresolved"
    results.loc[unresolved, "reconstructed_value"] = np.nan
    results.loc[unresolved, "modified"] = False
    results.loc[resolved & results["fault_type"].eq("missing"), "status"] = "Estimated"
    results.loc[resolved & results["fault_type"].ne("missing"), "status"] = "Corrected"
    results["acceptance_decision"] = "retained"
    results.loc[resolved, "acceptance_decision"] = "accepted"
    results.loc[unresolved, "acceptance_decision"] = "refused"
    results["decision_reason"] = "No fault diagnosed; measured value retained."
    results.loc[resolved, "decision_reason"] = (
        "Repair accepted: validation-calibrated confidence met the acceptance threshold."
    )
    results.loc[unresolved, "decision_reason"] = (
        "Repair refused: confidence was below the acceptance threshold; output withheld."
    )
    return results


def calibrate_run(results: pd.DataFrame) -> dict:
    if results.empty:
        return {
            "status": "no data",
            "success_probability": 0.0,
            "fault_count": 0,
            "modified_count": 0,
            "status_counts": {},
            "fault_counts": {},
        }

    calibrated = results["confidence"].astype(float)
    modified = results[results["modified"]]
    fault_counts = results.loc[results["fault_type"] != "healthy", "fault_type"].value_counts()
    status_counts = results["status"].value_counts()
    return {
        "status": "completed",
        "success_probability": round(float(modified["confidence"].mean()) if not modified.empty else float(calibrated.mean()), 2),
        "fault_count": int((results["fault_type"] != "healthy").sum()),
        "modified_count": int(len(modified)),
        "status_counts": {str(key): int(value) for key, value in status_counts.items()},
        "decision_counts": {
            str(key): int(value)
            for key, value in results["acceptance_decision"].value_counts().items()
        },
        "fault_counts": {str(key): int(value) for key, value in fault_counts.items()},
        "confidence_min": round(float(calibrated.min()), 2),
        "confidence_max": round(float(calibrated.max()), 2),
    }


if __name__ == "__main__":
    print(json.dumps(train_confidence_model(), indent=2))
