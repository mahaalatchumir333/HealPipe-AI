import json

from backend.service import evaluate_scenario
from pipeline.benchmark import GROUPS, _grid, run_benchmark


def test_benchmark_grid_respects_available_channels():
    cells = _grid()
    assert cells
    assert {cell["fault_type"] for cell in cells} == {"missing", "freeze", "spike", "drift"}
    assert all(cell["affected_channels"] <= len(GROUPS[cell["sensor_group"]]) for cell in cells)
    assert {cell["duration_seconds"] for cell in cells if cell["fault_type"] == "drift"} == {10, 20, 30}
    assert {cell["duration_seconds"] for cell in cells if cell["fault_type"] == "freeze"} == {1, 3, 5, 10}


def test_benchmark_writes_bounded_json_and_csv(tmp_path, monkeypatch):
    import pipeline.benchmark as benchmark

    monkeypatch.setattr(benchmark, "OUT", tmp_path)
    report = run_benchmark(repeats=1, max_cells=1)
    assert report["completed_cell_repeats"] == 1
    assert report["truncated"] is True
    assert (tmp_path / "benchmark.csv").exists()
    saved = json.loads((tmp_path / "benchmark.json").read_text())
    assert saved["cells"][0]["affected_channels"] == 1


def test_scenario_group_limits_injected_channels_to_that_group():
    result = evaluate_scenario(
        partition="validation",
        sensor="PS1",
        cycle_id=None,
        fault_type="freeze",
        start_second=20,
        duration_seconds=1,
        severity=6,
        affected_channels=3,
        sensor_group="pressure",
    )
    assert result["scenario"]["sensors"] == ["PS1", "PS2", "PS3"]
    assert result["scenario"]["sensor_group"] == "pressure"
