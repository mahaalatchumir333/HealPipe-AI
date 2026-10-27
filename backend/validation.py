import pandas as pd
from fastapi import HTTPException

from pipeline.reconstruct import SENSORS

MAX_UPLOAD_ROWS = 100_000


def validate_telemetry(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"cycle_id", "second_in_cycle", *SENSORS}
    missing = required.difference(frame.columns)
    if missing:
        raise HTTPException(status_code=422, detail=f"Upload is missing required columns: {sorted(missing)}")
    unexpected = set(frame.columns).difference(required)
    if unexpected:
        raise HTTPException(status_code=422, detail=f"Upload contains unsupported columns: {sorted(unexpected)}")
    if len(frame) > MAX_UPLOAD_ROWS:
        raise HTTPException(status_code=413, detail=f"Upload exceeds the {MAX_UPLOAD_ROWS:,}-row limit.")
    frame = frame[["cycle_id", "second_in_cycle", *SENSORS]].copy()
    for coordinate in ("cycle_id", "second_in_cycle"):
        numeric = pd.to_numeric(frame[coordinate], errors="coerce")
        if numeric.isna().any() or not numeric.mod(1).eq(0).all():
            raise HTTPException(status_code=422, detail=f"{coordinate} must contain integer values.")
        frame[coordinate] = numeric.astype(int)
    if not frame["second_in_cycle"].between(0, 59).all():
        raise HTTPException(status_code=422, detail="second_in_cycle must be in the range 0 to 59.")
    if frame.duplicated(["cycle_id", "second_in_cycle"]).any():
        raise HTTPException(status_code=422, detail="Each cycle/second coordinate must be unique.")
    for cycle_id, cycle in frame.groupby("cycle_id", sort=False):
        seconds = sorted(cycle["second_in_cycle"].tolist())
        if seconds != list(range(60)):
            raise HTTPException(
                status_code=422,
                detail=f"Cycle {cycle_id} must contain each second from 0 through 59 exactly once.",
            )
    for sensor in SENSORS:
        source = frame[sensor]
        numeric = pd.to_numeric(source, errors="coerce")
        invalid = source.notna() & numeric.isna()
        if invalid.any():
            raise HTTPException(status_code=422, detail=f"{sensor} must contain numeric values or missing data.")
        if numeric.dropna().isin([float("inf"), float("-inf")]).any():
            raise HTTPException(status_code=422, detail=f"{sensor} cannot contain infinite values.")
        frame[sensor] = numeric
    return frame.sort_values(["cycle_id", "second_in_cycle"]).reset_index(drop=True)
