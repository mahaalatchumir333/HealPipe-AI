from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ensure_compatibility_dirs() -> Path:
    """Support both the documented lowercase layout and the existing uppercase Data folder."""
    data_dir = ROOT / "data"
    legacy_data_dir = ROOT / "Data"

    if data_dir.exists():
        return data_dir
    if legacy_data_dir.exists():
        shutil.copytree(legacy_data_dir, data_dir, dirs_exist_ok=True)
        return data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def root_data_dir() -> Path:
    return ensure_compatibility_dirs()
