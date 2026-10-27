from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd

from backend.validation import validate_telemetry
from pipeline.reconstruct import SENSORS

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "artifacts" / "sensor_schema.json"


@dataclass(frozen=True)
class ChannelSchema:
    unit: str
    sampling_rate_hz: int


@dataclass(frozen=True)
class FeedSchema:
    processing_rate_hz: int
    channels: dict[str, ChannelSchema]


class RealTelemetryAdapter(Protocol):
    @property
    def schema(self) -> FeedSchema: ...

    def read_cycle(self) -> pd.DataFrame: ...


def expected_feed_schema() -> FeedSchema:
    config = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    channels = {
        name: ChannelSchema(
            unit=str(metadata["unit"]),
            sampling_rate_hz=int(metadata["sampling_rate_hz"]),
        )
        for name, metadata in config["channels"].items()
        if metadata.get("enabled") and not metadata.get("derived")
    }
    return FeedSchema(processing_rate_hz=1, channels=channels)


def validate_real_feed_cycle(adapter: RealTelemetryAdapter) -> pd.DataFrame:
    expected = expected_feed_schema()
    supplied = adapter.schema
    if supplied.processing_rate_hz != expected.processing_rate_hz:
        raise ValueError("Real feed processing grid must be one-second averages (1 Hz).")
    if set(supplied.channels) != set(expected.channels):
        raise ValueError(
            "Real feed channel schema does not match the enabled sensor schema; "
            "check channel names and opt-in retraining configuration."
        )
    for channel, required in expected.channels.items():
        actual = supplied.channels[channel]
        if actual.unit != required.unit:
            raise ValueError(
                f"Real feed unit mismatch for {channel}: expected {required.unit}, got {actual.unit}."
            )
        if actual.sampling_rate_hz != required.sampling_rate_hz:
            raise ValueError(
                f"Real feed sampling rate mismatch for {channel}: "
                f"expected {required.sampling_rate_hz} Hz, got {actual.sampling_rate_hz} Hz."
            )
    frame = validate_telemetry(adapter.read_cycle())
    if frame["cycle_id"].nunique() != 1 or len(frame) != 60:
        raise ValueError("A real feed adapter must provide exactly one complete 60-second cycle.")
    return frame


def load_replay_cycle(partition: str, cycle_id: int) -> pd.DataFrame:
    if partition not in {"validation", "test"}:
        raise ValueError("partition must be 'validation' or 'test'.")
    path = ROOT / "Data" / "corrupted" / f"{partition}_corrupted.parquet"
    frame = pd.read_parquet(path)
    cycle = frame[frame["cycle_id"] == cycle_id].sort_values("second_in_cycle")
    cycle = validate_telemetry(cycle[["cycle_id", "second_in_cycle", *SENSORS]])
    if len(cycle) != 60 or cycle["second_in_cycle"].tolist() != list(range(60)):
        raise ValueError(f"Cycle {cycle_id} is not a complete 60-second recorded cycle.")
    return cycle
