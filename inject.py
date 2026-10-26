"""
HealPipe AI - Step 2: reproducible fault injector.

Usage (from project root):
    python inject.py --partition validation --seed 101
    python inject.py --partition test --seed 202

Outputs (data/corrupted/):
    <partition>_corrupted.parquet        -> the ONLY file the repair pipeline may see
    <partition>_labels_private.parquet   -> hidden answer key (injected points only)
    <partition>_injection_manifest.json  -> settings, seed, counts
Also writes (once) artifacts/reference_scales.json, computed from TRAINING data only.

Untouched points are simply absent from the labels table (= no_injected_fault).
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from project_paths import root_data_dir

ROOT = Path(__file__).resolve().parent
DATA = root_data_dir()
SPLITS = DATA / "splits"
OUT = DATA / "corrupted"
ART = ROOT / "artifacts"
OUT.mkdir(parents=True, exist_ok=True)
ART.mkdir(parents=True, exist_ok=True)

SENSORS = ["PS1", "PS2", "PS3", "FS1", "FS2", "TS1", "TS2", "TS3", "TS4", "EPS1"]
SECS = 60
INJECTOR_VERSION = "v1"

# ---- benchmark settings (our own choices, NOT manufacturer failure limits) ----
BLOCK_LENGTHS = [1, 3, 5, 10]          # missing / freeze durations (seconds)
SPIKE_K = [3, 5, 8, 12]                # spike amplitude = +/- k * scale
DRIFT_LENGTHS = [10, 20, 30]           # drift window (seconds)
DRIFT_K = [3, 6, 10]                   # final drift offset = k * scale
CONTROL_FRACTION = 0.30                # cycles left completely untouched
MULTI_PROB = 0.15                      # event hits several sensors at once


def compute_scales():
    """s_k = 1.4826 * MAD of (value - per-second median profile), training only.
    Removes the normal within-cycle trend so the scale reflects residual noise,
    not the whole operating range."""
    path = ART / "reference_scales.json"
    if path.exists():
        return json.loads(path.read_text())
    train = pd.read_parquet(SPLITS / "train_reference.parquet")
    scales = {}
    for s in SENSORS:
        mat = train[s].to_numpy().reshape(-1, SECS)          # cycles x seconds
        resid = mat - np.median(mat, axis=0, keepdims=True)
        mad = np.median(np.abs(resid - np.median(resid)))
        scales[s] = float(max(1.4826 * mad, 1e-6))
    path.write_text(json.dumps(scales, indent=2))
    return scales


def inject(partition, seed):
    rng = np.random.default_rng(seed)
    scales = compute_scales()
    ref = pd.read_parquet(SPLITS / f"{partition}_reference.parquet")
    cycles = ref["cycle_id"].unique()
    obs = {s: ref[s].to_numpy().reshape(-1, SECS).copy() for s in SENSORS}
    refm = {s: ref[s].to_numpy().reshape(-1, SECS) for s in SENSORS}
    occupied = {s: np.zeros_like(obs[s], dtype=bool) for s in SENSORS}

    labels, event_id = [], 0
    for ci, cyc in enumerate(cycles):
        if rng.random() < CONTROL_FRACTION:
            continue                                      # untouched control cycle
        for _ in range(rng.integers(1, 4)):               # 1-3 events per cycle
            fault = rng.choice(["missing", "spike", "freeze", "drift"])
            n_sens = rng.integers(2, 4) if rng.random() < MULTI_PROB else 1
            targets = list(rng.choice(SENSORS, size=n_sens, replace=False))

            if fault in ("missing", "freeze"):
                length = int(rng.choice(BLOCK_LENGTHS)); severity = f"len={length}"
            elif fault == "spike":
                length = 1; k = float(rng.choice(SPIKE_K)); severity = f"k={k}"
            else:
                length = int(rng.choice(DRIFT_LENGTHS)); dk = float(rng.choice(DRIFT_K))
                severity = f"len={length},k={dk}"
            start = int(rng.integers(1, SECS - length + 1))   # start>=1: freeze needs a previous value
            end = start + length                              # exclusive
            sign = rng.choice([-1.0, 1.0])

            event_id += 1
            for s in targets:
                if occupied[s][ci, start - 1:end].any():
                    continue                                  # keep faults non-overlapping per sensor
                window = refm[s][ci, start:end]
                if fault == "freeze" and np.ptp(window) < 1e-9:
                    continue                                  # freeze on a flat signal = invisible fault
                if fault == "missing":
                    obs[s][ci, start:end] = np.nan
                elif fault == "spike":
                    obs[s][ci, start] = refm[s][ci, start] + sign * k * scales[s]
                elif fault == "freeze":
                    obs[s][ci, start:end] = refm[s][ci, start - 1]
                else:
                    ramp = np.linspace(0, 1, length + 1)[1:] * sign * dk * scales[s]
                    obs[s][ci, start:end] = refm[s][ci, start:end] + ramp
                occupied[s][ci, start - 1:end] = True
                for t in range(start, end):
                    labels.append({
                        "cycle_id": int(cyc), "second_in_cycle": t, "sensor_id": s,
                        "reference_value": float(refm[s][ci, t]),
                        "observed_value": None if np.isnan(obs[s][ci, t]) else float(obs[s][ci, t]),
                        "injected_fault": fault, "event_id": event_id,
                        "severity": severity, "seed": seed,
                        "n_sensors_in_event": len(targets),
                    })

    corrupted = ref[["cycle_id", "second_in_cycle"]].copy()
    for s in SENSORS:
        corrupted[s] = obs[s].reshape(-1)
    lab = pd.DataFrame(labels)
    if "corrupted_value" not in lab.columns:
        if "observed_value" in lab.columns:
            lab["corrupted_value"] = lab["observed_value"]
        else:
            lab["corrupted_value"] = None
    public_label_columns = [
        "cycle_id",
        "second_in_cycle",
        "sensor_id",
        "reference_value",
        "corrupted_value",
        "injected_fault",
    ]
    private_metadata_columns = [
        column
        for column in ("event_id", "severity", "seed", "n_sensors_in_event")
        if column in lab.columns
    ]
    lab = lab[public_label_columns + private_metadata_columns].copy()

    corrupted_path = OUT / f"{partition}_corrupted.parquet"
    labels_path = OUT / f"{partition}_labels_private.parquet"
    corrupted.to_parquet(corrupted_path, index=False)
    lab.to_parquet(labels_path, index=False)

    if partition == "test":
        corrupted.to_parquet(OUT / "test_corrupted.parquet", index=False)
        lab.to_parquet(OUT / "st_labels_private.parquet", index=False)
    elif partition == "validation":
        corrupted.to_parquet(OUT / "validation_corrupted.parquet", index=False)
        lab.to_parquet(OUT / "validation_labels_private.parquet", index=False)

    manifest = {
        "injector_version": INJECTOR_VERSION, "partition": partition, "seed": seed,
        "scales": scales, "block_lengths": BLOCK_LENGTHS, "spike_k": SPIKE_K,
        "drift_lengths": DRIFT_LENGTHS, "drift_k": DRIFT_K,
        "control_fraction": CONTROL_FRACTION, "multi_sensor_prob": MULTI_PROB,
        "events": int(event_id), "injected_points": int(len(lab)),
        "points_by_fault": lab["injected_fault"].value_counts().to_dict(),
        "total_sensor_points": int(len(ref) * len(SENSORS)),
    }
    (OUT / f"{partition}_injection_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"[{partition}] seed={seed}  events={event_id}  injected points={len(lab)} "
          f"of {manifest['total_sensor_points']}")
    print("   by fault:", manifest["points_by_fault"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", choices=["validation", "test"], required=True)
    ap.add_argument("--seed", type=int, required=True)
    a = ap.parse_args()
    inject(a.partition, a.seed)
