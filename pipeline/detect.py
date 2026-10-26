from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.reconstruct import SENSORS, feature_matrix

ROOT = Path(__file__).resolve().parent.parent
SCALE_PATH = ROOT / "artifacts" / "reference_scales.json"
MIN_REPAIR_CONFIDENCE = 50.0


def _sensor_scales() -> dict[str, float]:
    if SCALE_PATH.exists():
        return json.loads(SCALE_PATH.read_text())
    return {sensor: 1.0 for sensor in SENSORS}


def _freeze_candidate(values: np.ndarray, predictions: np.ndarray, scales: dict[str, float]) -> np.ndarray:
    flags = np.zeros(len(values), dtype=bool)
    for idx in range(len(values)):
        for window in (3, 5, 10):
            start = idx - window + 1
            if start < 0:
                continue
            observed = values[start:idx + 1]
            expected = predictions[start:idx + 1]
            if not np.all(np.isfinite(observed)) or np.ptp(observed) > scales["freeze_tolerance"]:
                continue
            expected_motion = np.ptp(expected)
            disagreement = np.max(np.abs(expected - observed))
            if expected_motion > scales["motion_tolerance"] and disagreement > scales["disagreement_tolerance"]:
                flags[start:idx + 1] = True
                break
    return flags


def _spike_scores(values: np.ndarray, scale: float) -> np.ndarray:
    scores = np.zeros(len(values), dtype=float)
    for idx, value in enumerate(values):
        if not np.isfinite(value):
            continue
        start = max(0, idx - 5)
        end = min(len(values), idx + 6)
        neighbors = np.concatenate((values[start:idx], values[idx + 1:end]))
        neighbors = neighbors[np.isfinite(neighbors)]
        if len(neighbors) < 2:
            continue
        median = float(np.median(neighbors))
        mad = float(np.median(np.abs(neighbors - median)))
        robust_scale = max(1.4826 * mad, scale * 0.12, 1e-8)
        scores[idx] = abs(value - median) / robust_scale
    return scores


def _drift_flags(residuals: np.ndarray, scale: float) -> np.ndarray:
    flags = np.zeros(len(residuals), dtype=bool)
    for idx in range(len(residuals)):
        start = idx - 9
        if start < 0:
            continue
        chunk = residuals[start:idx + 1]
        finite = np.isfinite(chunk)
        if finite.sum() < 9:
            continue
        chunk = chunk[finite]
        x = np.arange(len(chunk), dtype=float)
        slope, intercept = np.polyfit(x, chunk, 1)
        fitted = slope * x + intercept
        variance = float(np.sum((chunk - np.mean(chunk)) ** 2))
        r_squared = 1.0 - float(np.sum((chunk - fitted) ** 2)) / variance if variance > 0 else 0.0
        residual_magnitude = abs(float(chunk[-1])) / scale
        if (
            abs(float(slope)) >= 0.12 * scale
            and abs(float(chunk[-1] - chunk[0])) >= 0.75 * scale
            and residual_magnitude >= 1.0
            and r_squared >= 0.85
        ):
            flags[start:idx + 1] = True
    return flags


def _confidence(fault: str, evidence: float, local_scale: float) -> float:
    if fault == "missing":
        return 99.0
    if fault == "freeze":
        return float(np.clip(70.0 + evidence * 4.0, 70.0, 99.0))
    if fault == "spike":
        return float(np.clip(55.0 + (evidence - 6.0) * 4.0, 55.0, 99.0))
    if fault == "drift":
        return float(np.clip(55.0 + evidence * 5.0, 55.0, 95.0))
    return 100.0


def detect_faults(frame: pd.DataFrame, models: dict[str, object]) -> pd.DataFrame:
    """Diagnose, reconstruct, and score each sensor reading within its own cycle."""
    required = {"cycle_id", "second_in_cycle", *SENSORS}
    missing_columns = required.difference(frame.columns)
    if missing_columns:
        raise ValueError(f"Telemetry frame is missing required columns: {sorted(missing_columns)}")

    scales = _sensor_scales()
    rows: list[dict] = []

    ordered = frame.sort_values(["cycle_id", "second_in_cycle"], kind="stable").reset_index(drop=True)
    predictions_by_sensor: dict[str, np.ndarray] = {}
    for sensor in SENSORS:
        if sensor not in models:
            raise ValueError(f"No reconstruction model was supplied for {sensor}.")
        predictions_by_sensor[sensor] = models[sensor].predict(feature_matrix(ordered, sensor))

    for _, grouped_cycle in ordered.groupby("cycle_id", sort=False):
        positions = grouped_cycle.index.to_numpy()
        cycle = grouped_cycle.reset_index(drop=True)
        for sensor in SENSORS:
            values = cycle[sensor].to_numpy(dtype=float)
            predictions = predictions_by_sensor[sensor][positions]
            scale = max(float(scales.get(sensor, 1.0)), 1e-8)
            residuals = values - predictions

            fault_types = np.full(len(cycle), "healthy", dtype=object)
            evidence = np.zeros(len(cycle), dtype=float)

            missing = ~np.isfinite(values)
            fault_types[missing] = "missing"
            evidence[missing] = 10.0

            freeze_tolerance = max(scale * 0.015, 1e-8)
            freeze_flags = _freeze_candidate(
                values,
                predictions,
                {
                    "freeze_tolerance": freeze_tolerance,
                    "motion_tolerance": max(scale * 0.75, 1e-8),
                    "disagreement_tolerance": max(scale * 1.5, 1e-8),
                },
            )
            freeze_flags &= fault_types == "healthy"
            fault_types[freeze_flags] = "freeze"
            evidence[freeze_flags] = max(
                np.ptp(predictions[max(0, i - 4):i + 1]) / scale
                for i in np.flatnonzero(freeze_flags)
            ) if freeze_flags.any() else 0.0

            spike_scores = _spike_scores(values, scale)
            spike_flags = (
                (spike_scores > 6.0)
                & (np.abs(residuals) >= 3.0 * scale)
                & (fault_types == "healthy")
            )
            fault_types[spike_flags] = "spike"
            evidence[spike_flags] = spike_scores[spike_flags]

            drift_flags = _drift_flags(residuals, scale) & (fault_types == "healthy")
            fault_types[drift_flags] = "drift"
            evidence[drift_flags] = np.abs(residuals[drift_flags]) / scale

            interpolated = pd.Series(values).interpolate(limit_area="inside").to_numpy(dtype=float)
            repaired = values.copy()
            modified = fault_types != "healthy"
            missing_run_lengths = np.zeros(len(values), dtype=float)
            idx = 0
            while idx < len(values):
                if np.isfinite(values[idx]):
                    idx += 1
                    continue
                end = idx + 1
                while end < len(values) and not np.isfinite(values[end]):
                    end += 1
                missing_run_lengths[idx:end] = end - idx
                idx = end

            for idx in np.flatnonzero(modified):
                if fault_types[idx] == "missing" and np.isfinite(interpolated[idx]):
                    repaired[idx] = interpolated[idx]
                else:
                    repaired[idx] = predictions[idx]

            for idx, fault in enumerate(fault_types):
                confidence = _confidence(str(fault), evidence[idx], scale)
                status = "Measured"
                output_value = float(values[idx]) if np.isfinite(values[idx]) else None
                if fault != "healthy":
                    if confidence < MIN_REPAIR_CONFIDENCE or not np.isfinite(repaired[idx]):
                        status = "Unresolved"
                        output_value = None
                    else:
                        status = "Estimated" if fault == "missing" else "Corrected"
                        output_value = float(repaired[idx])

                reason = {
                    "healthy": "No fault evidence; original measurement retained.",
                    "missing": "Reading is non-finite; reconstructed from local interpolation or cross-sensor model.",
                    "freeze": "Repeated flat readings disagree with expected cross-sensor system behavior.",
                    "spike": "Reading is an isolated robust-MAD outlier relative to nearby samples.",
                    "drift": "Model residual has a persistent near-linear slope outside normal sensor variation.",
                }[str(fault)]
                supporting_sensors = [
                    candidate
                    for candidate in SENSORS
                    if candidate != sensor and np.isfinite(cycle[candidate].iloc[idx])
                ]
                repair_method = (
                    "linear_interpolation"
                    if fault == "missing" and np.isfinite(interpolated[idx])
                    else "cross_sensor_regression"
                    if fault != "healthy"
                    else "measured_input"
                )
                reasoning = (
                    f"{reason} "
                    f"{len(supporting_sensors)} other sensor channels were available; "
                    f"{repair_method.replace('_', ' ')} selected."
                )

                rows.append({
                    "cycle_id": int(cycle["cycle_id"].iloc[idx]),
                    "second_in_cycle": int(cycle["second_in_cycle"].iloc[idx]),
                    "sensor_id": sensor,
                    "observed_value": float(values[idx]) if np.isfinite(values[idx]) else None,
                    "prediction": float(predictions[idx]),
                    "reconstructed_value": output_value,
                    "residual": float(residuals[idx]) if np.isfinite(residuals[idx]) else None,
                    "fault_type": str(fault),
                    "diagnosis": reason,
                    "reasoning": reasoning,
                    "repair_method": repair_method,
                    "supporting_sensors": supporting_sensors,
                    "status": status,
                    "confidence": round(confidence, 2),
                    "modified": bool(status in {"Estimated", "Corrected"}),
                    "gap_length": int(missing_run_lengths[idx]),
                    "healthy_support": int(
                        np.isfinite(cycle.loc[idx, [other for other in SENSORS if other != sensor]].to_numpy(dtype=float)).sum()
                    ),
                    "prediction_disagreement": float(abs(residuals[idx]) / scale) if np.isfinite(residuals[idx]) else 0.0,
                    "evidence_strength": float(evidence[idx]),
                })

    return pd.DataFrame(rows)
