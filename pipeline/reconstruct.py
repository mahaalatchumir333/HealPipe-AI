from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from pipeline.sensor_schema import load_repair_channels

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
MODELS = ARTIFACTS / "models"
MODELS.mkdir(parents=True, exist_ok=True)
SENSOR_RATES = load_repair_channels()
SENSORS = list(SENSOR_RATES)
DATA_DIR = ROOT / "Data"


def get_reference_split(split_name: str = "train") -> pd.DataFrame:
    return pd.read_parquet(DATA_DIR / "splits" / f"{split_name}_reference.parquet")


def feature_matrix(df: pd.DataFrame, sensor: str) -> pd.DataFrame:
    other = [s for s in SENSORS if s != sensor]
    feat = df[[*other, "second_in_cycle"]].copy()
    for s in other:
        feat[f"{s}_missing"] = df[s].isna().astype(int)
    feat["second_in_cycle"] = df["second_in_cycle"].astype(float)
    return feat.fillna(0.0)


def ensure_models() -> dict[str, object]:
    missing_models = [sensor for sensor in SENSORS if not (MODELS / f"{sensor}.joblib").exists()]
    if not missing_models:
        return load_model_map()

    train = get_reference_split("train")
    models: dict[str, object] = {}
    for sensor in SENSORS:
        path = MODELS / f"{sensor}.joblib"
        if path.exists():
            models[sensor] = joblib.load(path)
            continue
        X = feature_matrix(train, sensor)
        y = train[sensor].fillna(train[sensor].median())
        model = HistGradientBoostingRegressor(
            learning_rate=0.05,
            max_depth=8,
            max_iter=200,
            random_state=42,
        )
        model.fit(X, y)
        joblib.dump(model, path)
        models[sensor] = model
    return models


@lru_cache(maxsize=1)
def load_model_map() -> dict[str, object]:
    models = {}
    for sensor in SENSORS:
        path = MODELS / f"{sensor}.joblib"
        if not path.exists():
            raise FileNotFoundError(f"Missing model for {sensor}. Run ensure_models() first.")
        models[sensor] = joblib.load(path)
    return models


def predict_dataframe(df: pd.DataFrame) -> dict[str, np.ndarray]:
    models = ensure_models()
    pred = {}
    for sensor in SENSORS:
        feat = feature_matrix(df, sensor)
        pred[sensor] = models[sensor].predict(feat)
    return pred


def residual_summary() -> dict:
    val = get_reference_split("validation")
    preds = predict_dataframe(val)
    residuals = {}
    for sensor in SENSORS:
        actual = val[sensor].to_numpy(dtype=float)
        residuals[sensor] = float(np.mean(np.abs(actual - preds[sensor])))
    return residuals
