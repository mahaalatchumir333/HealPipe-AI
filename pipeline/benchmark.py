from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.service import SENSOR_GROUPS, evaluate_scenario
from pipeline.reconstruct import SENSORS

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "artifacts" / "benchmark"
DEFAULT_SEED = 2601

GROUPS = SENSOR_GROUPS

FAULT_TYPE_CONFIG = {
    "missing": {"durations": [1, 3, 5, 10], "severities": [None]},
    "freeze": {"durations": [1, 3, 5, 10], "severities": [None]},
    "spike": {"durations": [1], "severities": [3, 5, 8, 12]},
    "drift": {"durations": [10, 20, 30], "severities": [3, 6, 10]},
}


def _grid() -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for fault_type, config in FAULT_TYPE_CONFIG.items():
        for duration_seconds in config["durations"]:
            for sensor_group, sensors in GROUPS.items():
                for affected_channels in (1, 2, 3):
                    if affected_channels > len(sensors):
                        continue
                    for severity in config["severities"]:
                        cells.append({
                            "fault_type": fault_type,
                            "sensor_group": sensor_group,
                            "affected_channels": affected_channels,
                            "duration_seconds": duration_seconds,
                            "severity": severity,
                        })
    return cells


def _estimate_total_cells() -> int:
    return len(_grid())


def _scenario_seed(seed: int, cell_index: int, repeat_index: int) -> int:
    return int(np.random.SeedSequence([seed, cell_index, repeat_index]).generate_state(1)[0])


def _run_cell(
    cell: dict[str, Any],
    repeat_index: int,
    cycle_id: int,
    seed: int,
) -> dict[str, Any]:
    fault_type = str(cell["fault_type"])
    duration = int(cell["duration_seconds"])
    max_start = 60 - (1 if fault_type == "spike" else duration)
    if max_start < 1:
        raise ValueError(f"Duration {duration} does not fit in a 60-second cycle.")
    run_seed = _scenario_seed(seed, int(cell["cell_index"]), repeat_index)
    start_second = 1 + run_seed % max_start
    sensors = GROUPS[str(cell["sensor_group"])]
    base_sensor = sensors[0]
    severity = float(cell["severity"]) if cell["severity"] is not None else 6.0
    scenario = evaluate_scenario(
        partition="validation",
        sensor=base_sensor,
        cycle_id=int(cycle_id),
        fault_type=fault_type,
        start_second=int(start_second),
        duration_seconds=duration,
        severity=severity,
        affected_channels=int(cell["affected_channels"]),
        sensor_group=str(cell["sensor_group"]),
    )
    metrics = scenario["metrics"]
    return {
        **{key: value for key, value in cell.items() if key != "cell_index"},
        "cell_index": int(cell["cell_index"]),
        "repeat_index": int(repeat_index),
        "seed": run_seed,
        "cycle_id": int(cycle_id),
        "start_second": int(start_second),
        "injected_points": int(metrics["injected_points"]),
        "correctly_diagnosed_points": int(metrics["correctly_diagnosed_points"]),
        "predicted_fault_class_points": int(metrics["predicted_fault_class_points"]),
        "accepted_repairs": int(metrics["accepted_repairs"]),
        "accepted_repairs_within_tolerance": int(metrics["accepted_repairs_within_tolerance"]),
        "unresolved_fault_points": int(metrics["unresolved_fault_points"]),
        "precision": float(metrics["diagnosis_precision"]) / 100.0,
        "recall": float(metrics["diagnosis_recall"]) / 100.0,
        "accepted_repair_coverage": (
            int(metrics["accepted_repairs"]) / max(int(metrics["injected_points"]), 1)
        ),
        "unresolved_rate": (
            int(metrics["unresolved_fault_points"]) / max(int(metrics["injected_points"]), 1)
        ),
        "normalized_error": metrics["normalized_repair_error"],
        "within_tolerance_rate": (
            int(metrics["accepted_repairs_within_tolerance"])
            / max(int(metrics["accepted_repairs"]), 1)
        ),
        "mean_confidence": metrics["mean_accepted_confidence"],
    }


def _aggregate_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not runs:
        return []
    frame = pd.DataFrame(runs)
    keys = [
        "cell_index", "fault_type", "sensor_group", "affected_channels",
        "duration_seconds", "severity",
    ]
    cells: list[dict[str, Any]] = []
    for key, group in frame.groupby(keys, dropna=False, sort=True):
        row = dict(zip(keys, key))
        if pd.isna(row["severity"]):
            row["severity"] = None
        injected = int(group["injected_points"].sum())
        correct = int(group["correctly_diagnosed_points"].sum())
        predicted = int(group["predicted_fault_class_points"].sum())
        accepted = int(group["accepted_repairs"].sum())
        accepted_success = int(group["accepted_repairs_within_tolerance"].sum())
        unresolved = int(group["unresolved_fault_points"].sum())
        error_weights = group["accepted_repairs"].fillna(0).to_numpy(dtype=float)
        errors = pd.to_numeric(group["normalized_error"], errors="coerce").to_numpy(dtype=float)
        valid_errors = np.isfinite(errors) & (error_weights > 0)
        confidence = pd.to_numeric(group["mean_confidence"], errors="coerce").to_numpy(dtype=float)
        valid_confidence = np.isfinite(confidence) & (error_weights > 0)
        row.update({
            "repeats": int(len(group)),
            "precision": correct / predicted if predicted else 0.0,
            "recall": correct / injected if injected else 0.0,
            "accepted_repair_coverage": accepted / injected if injected else 0.0,
            "unresolved_rate": unresolved / injected if injected else 0.0,
            "normalized_error": (
                float(np.average(errors[valid_errors], weights=error_weights[valid_errors]))
                if valid_errors.any() else None
            ),
            "within_tolerance_rate": accepted_success / accepted if accepted else 0.0,
            "mean_confidence": (
                float(np.average(confidence[valid_confidence], weights=error_weights[valid_confidence]))
                if valid_confidence.any() else None
            ),
            "injected_points": injected,
            "accepted_repairs": accepted,
            "unresolved_fault_points": unresolved,
        })
        cells.append(row)
    return cells


def _validation_cycles() -> list[int]:
    reference = pd.read_parquet(ROOT / "Data" / "splits" / "validation_reference.parquet")
    return sorted(int(value) for value in reference["cycle_id"].unique())


def run_benchmark(
    repeats: int = 3,
    max_cells: int | None = None,
    out_dir: Path | str | None = None,
    seed: int = DEFAULT_SEED,
) -> dict:
    if repeats < 1:
        raise ValueError("repeats must be at least one.")
    if max_cells is not None and max_cells < 1:
        raise ValueError("max_cells must be at least one.")
    full_grid = _grid()
    total_cells = len(full_grid)
    grid = full_grid[:max_cells] if max_cells is not None else full_grid
    cycles = _validation_cycles()
    if not cycles:
        raise ValueError("The validation partition contains no complete cycles.")

    started = time.perf_counter()
    runs = []
    for cell_index, source_cell in enumerate(grid):
        cell = {**source_cell, "cell_index": cell_index}
        cycle_rng = np.random.default_rng(_scenario_seed(seed, cell_index, 0))
        cycle_count = min(repeats, len(cycles))
        repeat_cycles = cycle_rng.choice(cycles, size=cycle_count, replace=False).tolist()
        for repeat_index in range(repeats):
            cycle_id = int(repeat_cycles[repeat_index % cycle_count])
            runs.append(_run_cell(cell, repeat_index, cycle_id, seed))

    cells = _aggregate_runs(runs)
    target_dir = Path(out_dir) if out_dir is not None else OUT
    target_dir.mkdir(parents=True, exist_ok=True)
    csv_path = target_dir / "benchmark.csv"
    json_path = target_dir / "benchmark.json"
    cell_frame = pd.DataFrame(cells)
    if cell_frame.empty:
        cell_frame = pd.DataFrame(columns=[
            "fault_type", "sensor_group", "affected_channels",
            "duration_seconds", "severity", "repeats", "precision",
            "recall", "accepted_repair_coverage", "unresolved_rate",
            "normalized_error", "within_tolerance_rate", "mean_confidence",
        ])
    cell_frame.to_csv(csv_path, index=False)
    report = {
        "benchmark_version": "validation-grid-v1",
        "partition": "validation",
        "seed": int(seed),
        "severity_grid": {
            "spike": FAULT_TYPE_CONFIG["spike"]["severities"],
            "drift": FAULT_TYPE_CONFIG["drift"]["severities"],
            "missing_freeze": "Not applicable; duration is the severity dimension.",
        },
        "duration_grid_seconds": {
            fault: config["durations"] for fault, config in FAULT_TYPE_CONFIG.items()
        },
        "sensor_groups": GROUPS,
        "available_cells_only": True,
        "runs": runs,
        "cells": cells,
        "completed_cell_repeats": len(runs),
        "total_cells": len(grid),
        "full_grid_cells": total_cells,
        "repeats": repeats,
        "truncated": len(grid) < total_cells,
        "estimated_total_runs": total_cells * repeats,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "metric_definitions": {
            "precision": "Correct fault-class diagnoses divided by predicted diagnoses of that class.",
            "recall": "Correct fault-class diagnoses divided by injected points.",
            "accepted_repair_coverage": "Accepted injected-point repairs divided by injected points.",
            "unresolved_rate": "Unresolved injected points divided by injected points.",
            "normalized_error": "Accepted repair absolute error divided by robust training reference scale, aggregated across accepted points.",
            "within_tolerance_rate": "Accepted repairs within 1.5x robust training reference scale divided by accepted repairs.",
            "mean_confidence": "Mean confidence among accepted repairs.",
        },
        "grid_note": (
            "Only channel counts supported by each group are included: the dataset has one power "
            "channel and two flow channels. Each repeat uses a distinct deterministically selected "
            "validation cycle when enough cycles are available. One-second freeze cells are included "
            "as inherently ambiguous single-sample cases; they are reported, not tuned as detectable."
        ),
    }
    json_path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark fault type, duration, severity, channel group, and affected-channel count on validation cycles."
    )
    parser.add_argument("--repeats", type=int, default=3, help="Number of distinct validation-cycle repeats per cell.")
    parser.add_argument("--max-cells", type=int, default=None, help="Optional cap for a bounded pilot run.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Fixed seed for deterministic cycle and start selection.")
    parser.add_argument("--out-dir", type=str, default=str(OUT), help="Directory for benchmark.csv and benchmark.json.")
    args = parser.parse_args()
    estimate_runs = _estimate_total_cells() * args.repeats
    rough_minutes = estimate_runs / 60.0
    print(
        f"Benchmark estimate: {estimate_runs} scenario runs across "
        f"{_estimate_total_cells()} cells; rough wall time at 1 run/second: "
        f"{rough_minutes:.1f} minutes. Use --max-cells for a pilot."
    )
    report = run_benchmark(
        repeats=args.repeats,
        max_cells=args.max_cells,
        out_dir=args.out_dir,
        seed=args.seed,
    )
    print(json.dumps({
        "completed_cell_repeats": report["completed_cell_repeats"],
        "cells": report["total_cells"],
        "elapsed_seconds": report["elapsed_seconds"],
    }, indent=2))


if __name__ == "__main__":
    main()
