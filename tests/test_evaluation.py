import pandas as pd

from pipeline.evaluate import _baseline_metrics


def test_baseline_comparison_masks_oracle_fault_point_only():
    fault_points = pd.DataFrame([{
        "cycle_id": 1,
        "second_in_cycle": 1,
        "sensor_id": "PS1",
        "reference_value": 20.0,
        "truth": "spike",
    }])
    detections = pd.DataFrame([{
        "cycle_id": 1,
        "second_in_cycle": 1,
        "sensor_id": "PS1",
        "observed_value": 200.0,
        "prediction": 21.0,
        "reconstructed_value": 21.0,
        "fault_type": "spike",
        "status": "Corrected",
    }])
    corrupted = pd.DataFrame({
        "cycle_id": [1, 1, 1],
        "second_in_cycle": [0, 1, 2],
        "PS1": [10.0, 200.0, 30.0],
    })

    results = _baseline_metrics(fault_points, detections, corrupted)

    assert results["forward_fill"]["mae"] == 10.0
    assert results["linear_interpolation"]["mae"] == 0.0
    assert results["cross_sensor_model"]["mae"] == 1.0
