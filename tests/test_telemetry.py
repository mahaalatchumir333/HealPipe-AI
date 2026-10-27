import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend import database
from backend.main import app
from backend.telemetry import (
    ChannelSchema,
    FeedSchema,
    expected_feed_schema,
    validate_real_feed_cycle,
)
from pipeline.reconstruct import SENSORS


class RecordedAdapter:
    def __init__(self, schema, frame):
        self.schema = schema
        self.frame = frame

    def read_cycle(self):
        return self.frame


def _one_cycle():
    return pd.DataFrame({
        "cycle_id": [7] * 60,
        "second_in_cycle": list(range(60)),
        **{sensor: [1.0] * 60 for sensor in SENSORS},
    })


def test_real_feed_adapter_validates_grid_units_rates_and_cycle():
    schema = expected_feed_schema()
    result = validate_real_feed_cycle(RecordedAdapter(schema, _one_cycle()))
    assert len(result) == 60
    assert schema.processing_rate_hz == 1
    assert "CE" not in schema.channels
    assert "CP" not in schema.channels
    assert "SE" not in schema.channels

    wrong_units = dict(schema.channels)
    wrong_units["PS1"] = ChannelSchema(unit="kPa", sampling_rate_hz=100)
    with pytest.raises(ValueError, match="unit mismatch"):
        validate_real_feed_cycle(RecordedAdapter(
            FeedSchema(processing_rate_hz=1, channels=wrong_units),
            _one_cycle(),
        ))


def test_replay_endpoint_streams_ticks_then_completed_offline_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "telemetry.db")
    monkeypatch.setattr("backend.main.load_replay_cycle", lambda partition, cycle_id: _one_cycle())
    monkeypatch.setattr(
        "backend.main.evaluation_series",
        lambda partition, sensor, cycle_id, include_reference: {
            "sensor": sensor,
            "partition": partition,
            "selected_cycle": cycle_id,
            "observed": [1.0],
            "repaired": [1.0],
            "faults": ["healthy"],
            "statuses": ["Measured"],
            "confidence": [100.0],
            "points": [{"cycle_id": cycle_id, "second_in_cycle": 0, "sensor_id": sensor, "status": "Measured", "fault_type": "healthy"}],
        },
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/telemetry/replay?partition=validation&cycle_id=7&sensor_id=PS1&interval_ms=0",
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: tick") == 60
    assert '"clock_label": "SIMULATED"' in response.text
    assert "event: cycle_complete" in response.text
    assert "event: done" in response.text
