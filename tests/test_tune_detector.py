import pandas as pd

from pipeline.tune_detector import _metrics, _severity_parts, _stratified_metrics


def test_severity_parser_separates_duration_and_sensor_scale():
    assert _severity_parts("len=20,k=6.0", "drift") == (20, 6.0)
    assert _severity_parts("len=5", "freeze") == (5, None)
    assert _severity_parts("k=8.0", "spike") == (1, 8.0)


def test_validation_metrics_report_faults_untouched_cycles_and_one_second_freeze():
    frame = pd.DataFrame([
        {
            "cycle_id": 1,
            "second_in_cycle": 10,
            "sensor_id": "PS1",
            "truth": "freeze",
            "severity": "len=1",
            "event_id": 1,
            "fault_type": "healthy",
        },
        {
            "cycle_id": 2,
            "second_in_cycle": 10,
            "sensor_id": "PS1",
            "truth": "healthy",
            "severity": None,
            "event_id": None,
            "fault_type": "spike",
        },
        {
            "cycle_id": 3,
            "second_in_cycle": 10,
            "sensor_id": "PS1",
            "truth": "drift",
            "severity": "len=10,k=3.0",
            "event_id": 2,
            "fault_type": "drift",
        },
    ])

    metrics = _metrics(frame)
    stratified = _stratified_metrics(frame)

    assert metrics["per_fault"]["freeze"]["recall"] == 0.0
    assert metrics["untouched_cycles"]["count"] == 1
    assert metrics["untouched_cycles"]["false_positive_cycle_rate"] == 1.0
    assert stratified["inherently_undetectable"][0]["validation_examples"] == 1
    assert stratified["by_fault_duration_severity"][0]["fault_type"] == "drift"
