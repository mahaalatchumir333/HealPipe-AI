from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "artifacts" / "sensor_schema.json"
DERIVED_CHANNELS = {"CE", "CP", "SE"}


def load_repair_channels() -> dict[str, int]:
    config = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    channels = config.get("channels")
    if not isinstance(channels, dict):
        raise ValueError(f"Sensor schema at {SCHEMA_PATH} must define a channels object.")
    optional_config = config.get("optional_channels_require_retraining", {})
    optional_channels = set(optional_config.get("channels", []))
    optional_enabled = bool(optional_config.get("enabled", False))
    selected: dict[str, int] = {}
    for channel, metadata in channels.items():
        if channel in DERIVED_CHANNELS:
            if metadata.get("enabled"):
                raise ValueError(f"Derived sensor {channel} cannot be enabled as a repair input.")
            continue
        if channel in optional_channels and metadata.get("enabled") and not optional_enabled:
            raise ValueError(
                f"Optional channel {channel} requires optional_channels_require_retraining.enabled=true."
            )
        if not metadata.get("enabled"):
            continue
        filename = metadata.get("file")
        rate = metadata.get("sampling_rate_hz")
        if not isinstance(filename, str) or not filename.endswith(".txt"):
            raise ValueError(f"Sensor {channel} must name a raw .txt data file.")
        if not isinstance(rate, int) or rate < 1:
            raise ValueError(f"Sensor {channel} must have a positive integer sampling_rate_hz.")
        selected[str(channel)] = rate
    if not selected:
        raise ValueError("Sensor schema enables no repair-supported channels.")
    return selected
