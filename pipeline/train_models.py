from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import GroupKFold

from pipeline.reconstruct import ARTIFACTS, MODELS, SENSORS, feature_matrix, get_reference_split


def _new_model() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        learning_rate=0.05,
        max_depth=8,
        max_iter=200,
        random_state=42,
    )


def train_all(n_splits: int = 5) -> dict:
    train = get_reference_split("train")
    groups = train["cycle_id"].to_numpy()
    actual_splits = min(n_splits, len(np.unique(groups)))
    if actual_splits < 2:
        raise ValueError("At least two distinct training cycles are required for cross-fitting.")

    cross_fitter = GroupKFold(n_splits=actual_splits)
    residual_summary: dict[str, dict] = {}
    for sensor in SENSORS:
        X = feature_matrix(train, sensor)
        y = train[sensor].to_numpy(dtype=float)
        predictions = np.full(len(train), np.nan, dtype=float)

        for train_idx, holdout_idx in cross_fitter.split(X, y, groups):
            fold_model = _new_model()
            fold_model.fit(X.iloc[train_idx], y[train_idx])
            predictions[holdout_idx] = fold_model.predict(X.iloc[holdout_idx])

        residuals = y - predictions
        finite_residuals = residuals[np.isfinite(residuals)]
        if len(finite_residuals) != len(y):
            raise RuntimeError(f"Cross-fitted residuals for {sensor} are incomplete.")

        final_model = _new_model()
        final_model.fit(X, y)
        joblib.dump(final_model, MODELS / f"{sensor}.joblib")

        residual_summary[sensor] = {
            "count": int(len(finite_residuals)),
            "mean": float(np.mean(finite_residuals)),
            "std": float(np.std(finite_residuals)),
            "mae": float(np.mean(np.abs(finite_residuals))),
            "scale": float(max(1.4826 * np.median(
                np.abs(finite_residuals - np.median(finite_residuals))
            ), 1e-8)),
            "quantiles": {
                str(q): float(np.quantile(finite_residuals, q))
                for q in (0.01, 0.05, 0.5, 0.95, 0.99)
            },
        }

    output = {
        "method": "GroupKFold by cycle_id",
        "folds": actual_splits,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sensors": residual_summary,
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "residual_reference.json").write_text(json.dumps(output, indent=2))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cross-fit and train per-sensor reconstruction models.")
    parser.add_argument("--folds", type=int, default=5)
    arguments = parser.parse_args()
    result = train_all(arguments.folds)
    print(json.dumps({
        "folds": result["folds"],
        "sensors": list(result["sensors"]),
        "residual_reference": str(ARTIFACTS / "residual_reference.json"),
    }, indent=2))
