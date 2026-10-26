from pathlib import Path

import pandas as pd

from backend.celery_app import celery_app
from backend.database import update_job
from backend.service import create_run_record, create_upload_run
from backend.validation import validate_telemetry


@celery_app.task(name="healpipe.process_partition")
def process_partition_task(job_id: str, owner_id: int, request_data: dict) -> int:
    try:
        update_job(job_id, "running")
        name = request_data["name"]
        partition = request_data["partition"]
        run = create_run_record(name, partition, owner_id)
        update_job(job_id, "completed", run_id=run["run_id"])
        return run["run_id"]
    except Exception as exc:
        update_job(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise


@celery_app.task(name="healpipe.process_upload")
def process_upload_task(job_id: str, owner_id: int, name: str, upload_path: str) -> int:
    try:
        update_job(job_id, "running")
        path = Path(upload_path)
        if path.suffix.lower() == ".csv":
            frame = pd.read_csv(path)
        else:
            frame = pd.read_parquet(path)
        frame = validate_telemetry(frame)
        run = create_upload_run(name, frame, owner_id)
        update_job(job_id, "completed", run_id=run["run_id"])
        return run["run_id"]
    except Exception as exc:
        update_job(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise
