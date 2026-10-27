"""
HealPipe AI - Step 1: ingest, validate, aggregate to 1 Hz, split.

Run from the project root (C:\\Users\\Dell\\Desktop\\HealPipe-AI):
    python prepare_data.py

Expects the extracted Kaggle files (PS1.txt ... profile.txt) in data/raw/
Outputs:
    data/raw/manifest.json
    data/reference/validation_report.json
    data/reference/reference_1hz.parquet
    data/splits/{train,validation,test}_reference.parquet
    data/splits/split_manifest.json
"""
import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from project_paths import root_data_dir
from pipeline.sensor_schema import load_repair_channels

ROOT = Path(__file__).resolve().parent
DATA = root_data_dir()
RAW = DATA / "raw"
REF = DATA / "reference"
SPLITS = DATA / "splits"
for d in (RAW, REF, SPLITS):
    d.mkdir(parents=True, exist_ok=True)

# sensor -> samples per second (Hz), configured in artifacts/sensor_schema.json
SENSORS = load_repair_channels()
CYCLE_SECONDS = 60
EXPECTED_CYCLES = 2205


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- 1. manifest ----------
def write_manifest():
    files = sorted(list(RAW.glob("*.txt")))
    manifest = {
        "dataset_name": "Condition monitoring of hydraulic systems",
        "source": "Kaggle mirror of UCI dataset 447",
        "download_date": str(date.today()),
        "license": "CC BY 4.0 (credit the dataset creators)",
        "files": [
            {"file_name": p.name, "file_hash": sha256(p), "file_size": p.stat().st_size}
            for p in files
        ],
    }
    (RAW / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"[1] manifest written ({len(files)} files)")


# ---------- 2. validate + 3. aggregate ----------
def load_and_validate():
    report = {"checks": [], "failures": [], "channels": {}}
    aligned = {}

    for sensor, hz in SENSORS.items():
        path = RAW / f"{sensor}.txt"
        if not path.exists():
            report["failures"].append(f"{sensor}: file missing")
            continue
        m = np.loadtxt(path)
        expected_cols = CYCLE_SECONDS * hz
        info = {
            "shape": list(m.shape),
            "expected_cols": expected_cols,
            "nan_count": int(np.isnan(m).sum()),
            "inf_count": int(np.isinf(m).sum()),
        }
        report["channels"][sensor] = info

        if m.shape[0] != EXPECTED_CYCLES:
            report["failures"].append(f"{sensor}: {m.shape[0]} cycles, expected {EXPECTED_CYCLES}")
        if m.shape[1] != expected_cols:
            report["failures"].append(f"{sensor}: {m.shape[1]} columns, expected {expected_cols}")
            continue
        if info["nan_count"] or info["inf_count"]:
            report["failures"].append(f"{sensor}: contains NaN/inf")

        # X[c,t] = (1/fs) * sum of raw readings in second t of cycle c
        aligned[sensor] = m.reshape(m.shape[0], CYCLE_SECONDS, hz).mean(axis=2)
        report["checks"].append(f"{sensor}: OK ({m.shape})")

    profile_path = RAW / "profile.txt"
    n_profile = None
    if profile_path.exists():
        profile = np.loadtxt(profile_path)
        n_profile = profile.shape[0]
        if n_profile != EXPECTED_CYCLES:
            report["failures"].append(f"profile.txt has {n_profile} rows")
    else:
        profile = None
        report["failures"].append("profile.txt missing")

    (REF / "validation_report.json").write_text(json.dumps(report, indent=2))
    print(f"[2] validation done, failures: {len(report['failures'])}")
    if report["failures"]:
        for f in report["failures"]:
            print("    -", f)
        raise SystemExit("Fix the failures above before continuing.")
    return aligned, profile


def build_reference(aligned, profile):
    n_cycles = next(iter(aligned.values())).shape[0]
    cycle_id = np.repeat(np.arange(1, n_cycles + 1), CYCLE_SECONDS)
    second = np.tile(np.arange(CYCLE_SECONDS), n_cycles)
    df = pd.DataFrame({"cycle_id": cycle_id, "second_in_cycle": second})
    for sensor in SENSORS:
        df[sensor] = aligned[sensor].reshape(-1)  # row-major: cycle then second

    df.to_parquet(REF / "reference_1hz.parquet", index=False)
    print(f"[3] reference_1hz.parquet: {df.shape[0]} rows x {df.shape[1]} cols")

    if profile is not None:
        cols = ["cooler", "valve", "pump", "accumulator", "stable_flag"]
        prof = pd.DataFrame(profile, columns=cols)
        prof.insert(0, "cycle_id", np.arange(1, n_cycles + 1))
        prof.to_parquet(REF / "profile_metadata.parquet", index=False)
    return df, n_cycles


# ---------- 4. split by whole cycles ----------
def split(df, n_cycles):
    n_train = int(n_cycles * 0.6)
    n_val = int(n_cycles * 0.2)
    bounds = {
        "train": (1, n_train),
        "validation": (n_train + 1, n_train + n_val),
        "test": (n_train + n_val + 1, n_cycles),
    }
    manifest = {}
    for name, (lo, hi) in bounds.items():
        part = df[(df.cycle_id >= lo) & (df.cycle_id <= hi)]
        part.to_parquet(SPLITS / f"{name}_reference.parquet", index=False)
        manifest[name] = {"first_cycle": lo, "last_cycle": hi, "rows": len(part)}
    (SPLITS / "split_manifest.json").write_text(json.dumps(manifest, indent=2))
    print("[4] splits:", manifest)


if __name__ == "__main__":
    write_manifest()
    aligned, profile = load_and_validate()
    df, n = build_reference(aligned, profile)
    split(df, n)
    print("\nDone. Next: inject.py (fault injector + hidden labels).")
