from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator

from backend.auth import (
    bearer,
    current_user,
    issue_token,
    new_totp_secret,
    optional_user,
    register,
    verify_password,
    verify_totp,
)
from backend.database import (
    create_job,
    get_job,
    get_run,
    get_user as get_user_record,
    init_db,
    insert_run,
    list_runs,
    update_job,
    update_user_mfa,
)
from backend.service import (
    create_run_record,
    create_upload_run,
    current_metrics,
    evaluation_series,
    evaluation_rig,
    evaluate_scenario,
    load_sensor_map,
    load_evaluation,
    load_benchmark,
    select_run_series_cycle,
    sample_chart_points,
)
from backend.validation import validate_telemetry
from backend.telemetry import load_replay_cycle
from pipeline.reconstruct import SENSORS, SENSOR_RATES, load_model_map

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
UPLOADS = Path(
    os.getenv(
        "HEALPIPE_UPLOADS_PATH",
        str(ROOT / "Data" / "uploads"),
    )
)

UPLOADS.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOADS.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    load_model_map()
    if not (ROOT / "artifacts" / "calibration" / "confidence.joblib").exists():
        raise RuntimeError("Confidence calibration artifact is missing; run `python -m pipeline.calibrate`.")
    yield


app = FastAPI(title="HealPipe AI", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


class RunRequest(BaseModel):
    name: str = Field(default="dashboard-run", min_length=1, max_length=100)
    partition: str = Field(default="validation", pattern="^(validation|test)$")


class ScenarioRequest(BaseModel):
    partition: str = Field(default="validation", pattern="^(validation|test)$")
    sensor_id: str = Field(default="PS1")
    cycle_id: int | None = Field(default=None, ge=0)
    fault_type: str = Field(pattern="^(missing|freeze|spike|drift)$")
    start_second: int = Field(default=20, ge=1, le=59)
    duration_seconds: int = Field(default=10, ge=1, le=30)
    severity: float = Field(default=6.0, ge=1.0, le=12.0)
    affected_channels: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def validate_scenario_parameters(self):
        if self.fault_type == "freeze" and not 3 <= self.duration_seconds <= 10:
            raise ValueError("Freeze duration must be between 3 and 10 seconds.")
        if self.fault_type == "drift" and not 10 <= self.duration_seconds <= 30:
            raise ValueError("Drift duration must be between 10 and 30 seconds.")
        if self.fault_type in {"spike", "drift"} and self.severity < 3:
            raise ValueError("Spike and drift severity must be at least 3 sensor scales.")
        effective_duration = 1 if self.fault_type == "spike" else self.duration_seconds
        if self.start_second + effective_duration > 60:
            raise ValueError("The fault duration must fit within the selected 60-second cycle.")
        return self


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(RegisterRequest):
    mfa_code: str | None = Field(default=None, pattern=r"^\d{6}$")


class MfaCodeRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


def _process_partition_job(job_id: str, owner_id: int, request: RunRequest) -> None:
    try:
        update_job(job_id, "running")
        run = create_run_record(request.name, request.partition, owner_id)
        update_job(job_id, "completed", run_id=run["run_id"])
    except Exception as exc:
        update_job(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise


def _process_upload_job(job_id: str, owner_id: int | None, name: str, path: Path) -> None:
    try:
        update_job(job_id, "running")
        if path.suffix.lower() == ".csv":
            frame = pd.read_csv(path)
        else:
            frame = pd.read_parquet(path)
        frame = validate_telemetry(frame)
        run = create_upload_run(name, frame, owner_id)
        update_job(job_id, "completed", run_id=run["run_id"])
    except Exception as exc:
        update_job(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def home():
    return TEMPLATES.get_template("index.html").render()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return TEMPLATES.get_template("dashboard.html").render()


@app.get("/api/summary")
def api_summary() -> dict:
    return current_metrics()


@app.get("/api/config")
def api_config() -> dict:
    schema = json.loads((ROOT / "artifacts" / "sensor_schema.json").read_text(encoding="utf-8"))
    sensor_map = json.loads((ROOT / "artifacts" / "sensor_map.json").read_text(encoding="utf-8"))["sensors"]
    return {
        "demo_upload_enabled": os.getenv("DEMO_MODE") == "1",
        "processing_grid": "1 Hz; one-second averages",
        "sensors": [
            {
                "sensor_id": sensor,
                "unit": schema["channels"][sensor]["unit"],
                "sampling_rate_hz": SENSOR_RATES[sensor],
                "sensor_type": sensor_map.get(sensor, {}).get("sensor_type", "sensor"),
            }
            for sensor in SENSORS
        ],
    }


@app.post("/auth/register", status_code=201)
def auth_register(request: RegisterRequest) -> dict:
    user_id = register(request.username, request.password)
    return {"id": user_id, "username": request.username.strip().lower(), "message": "Account created."}


@app.post("/auth/login")
def auth_login(request: LoginRequest) -> dict:
    user = get_user_record(request.username.strip().lower())
    if user is None or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    if user["mfa_enabled"]:
        if not request.mfa_code or not verify_totp(user["mfa_secret"], request.mfa_code):
            raise HTTPException(status_code=401, detail="A valid MFA code is required.")
    return {
        "access_token": issue_token(user["username"], user["id"]),
        "token_type": "bearer",
        "expires_in": 3600,
        "mfa_enabled": bool(user["mfa_enabled"]),
    }


@app.post("/auth/mfa/setup")
def auth_mfa_setup(user: dict = Depends(current_user)) -> dict:
    account = get_user_record(user["username"])
    if account and account["mfa_enabled"]:
        raise HTTPException(status_code=409, detail="MFA is already enabled.")
    secret = new_totp_secret()
    update_user_mfa(user["username"], None, False, pending=secret)
    uri = f"otpauth://totp/HealPipe%20AI:{user['username']}?secret={secret}&issuer=HealPipe%20AI"
    return {"secret": secret, "otpauth_uri": uri}


@app.post("/auth/mfa/confirm")
def auth_mfa_confirm(request: MfaCodeRequest, user: dict = Depends(current_user)) -> dict:
    account = get_user_record(user["username"])
    pending_secret = account["pending_mfa_secret"] if account else None
    if not pending_secret or not verify_totp(pending_secret, request.code):
        raise HTTPException(status_code=400, detail="MFA setup code is invalid or expired.")
    update_user_mfa(user["username"], pending_secret, True)
    return {"mfa_enabled": True}


@app.get("/api/runs")
def api_runs(user: dict = Depends(current_user)) -> dict:
    rows = list_runs(user["id"])
    return {"runs": rows}


@app.get("/api/chart")
def api_chart() -> dict:
    return sample_chart_points()


@app.get("/api/evaluation")
def api_evaluation(
    partition: str = Query(default="test", pattern="^(validation|test|test_v2)$"),
) -> dict:
    try:
        return load_evaluation(partition)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/benchmark")
def api_benchmark() -> dict:
    try:
        return load_benchmark()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/scenarios/evaluate")
def api_evaluate_scenario(
    request: ScenarioRequest,
    user: dict | None = Depends(optional_user),
) -> dict:
    try:
        result = evaluate_scenario(
            request.partition,
            request.sensor_id,
            request.cycle_id,
            request.fault_type,
            request.start_second,
            request.duration_seconds,
            request.severity,
            request.affected_channels,
        )
        metrics = {
            **result["metrics"],
            "scenario_parameters": result["scenario"],
        }
        run_id = insert_run(
            name=f"{request.fault_type} scenario",
            partition=request.partition,
            status="completed",
            metrics=metrics,
            events=result["fault_audit"],
            series={request.sensor_id: result["series"]},
            owner_id=user["id"] if user else None,
        )
        result["run_id"] = run_id
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scenarios/runs/{run_id}/audit")
def scenario_run_audit(
    run_id: int,
    user: dict | None = Depends(optional_user),
) -> Response:
    run = get_run(run_id, user["id"] if user else None)
    if run is None or "scenario_parameters" not in run["metrics"]:
        raise HTTPException(status_code=404, detail="Scenario run not found.")
    content = "\n".join(
        json.dumps(event, allow_nan=False) for event in run["events"]
    )
    return Response(
        content=f"{content}\n" if content else "",
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="healpipe-scenario-{run_id}.jsonl"'},
    )


@app.get("/api/scenarios/runs/{run_id}")
def get_scenario_run(
    run_id: int,
    user: dict | None = Depends(optional_user),
) -> dict:
    run = get_run(run_id, user["id"] if user else None)
    if run is None or "scenario_parameters" not in run["metrics"]:
        raise HTTPException(status_code=404, detail="Scenario run not found.")
    return {
        "run_id": run["id"],
        "name": run["name"],
        "partition": run["partition"],
        "status": run["status"],
        "created_at": run["created_at"],
        "scenario": run["metrics"]["scenario_parameters"],
        "metrics": {
            key: value
            for key, value in run["metrics"].items()
            if key != "scenario_parameters"
        },
        "fault_audit": run["events"],
        "series": next(iter(run["series"].values()), {}),
    }


@app.get("/api/evaluation/series")
def api_evaluation_series(
    partition: str = Query(default="test", pattern="^(validation|test)$"),
    sensor_id: str = Query(default="PS1"),
    cycle_id: int | None = Query(default=None, ge=0),
    include_reference: bool = Query(default=False),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict:
    user = None
    if include_reference:
        user = current_user(credentials)
    if include_reference and user is None:
        raise HTTPException(status_code=401, detail="Authentication is required to reveal reference values.")
    try:
        return evaluation_series(partition, sensor_id, cycle_id, include_reference)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/evaluation/rig")
def api_evaluation_rig(
    partition: str = Query(default="test", pattern="^(validation|test)$"),
    cycle_id: int = Query(ge=0),
) -> dict:
    try:
        return evaluation_rig(partition, cycle_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/telemetry/replay")
async def api_telemetry_replay(
    partition: str = Query(default="test", pattern="^(validation|test)$"),
    cycle_id: int = Query(ge=0),
    sensor_id: str = Query(default="PS1"),
    interval_ms: int = Query(default=1000, ge=0, le=5000),
) -> StreamingResponse:
    if sensor_id not in SENSORS:
        raise HTTPException(status_code=422, detail=f"sensor_id must be one of {SENSORS}.")
    try:
        recorded_cycle = load_replay_cycle(partition, cycle_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def safe_value(value):
        if hasattr(value, "item"):
            value = value.item()
        if isinstance(value, float) and not pd.notna(value):
            return None
        if isinstance(value, dict):
            return {key: safe_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [safe_value(item) for item in value]
        return value

    async def events():
        for row in recorded_cycle.to_dict(orient="records"):
            second = int(row["second_in_cycle"])
            payload = {
                "clock_label": "SIMULATED",
                "cycle_id": int(cycle_id),
                "second_in_cycle": second,
                "simulated_time": f"00:{second:02d}",
                "samples": {
                    channel: safe_value(row[channel])
                    for channel in SENSORS
                },
                "repair_status": "pending_until_cycle_complete",
            }
            yield f"event: tick\ndata: {json.dumps(payload, allow_nan=False)}\n\n"
            if interval_ms:
                await asyncio.sleep(interval_ms / 1000)

        series = evaluation_series(partition, sensor_id, cycle_id, False)
        points = series["points"]
        statuses: dict[str, int] = {}
        for point in points:
            statuses[point["status"]] = statuses.get(point["status"], 0) + 1
        completed = {
            "clock_label": "SIMULATED",
            "cycle_id": int(cycle_id),
            "sensor_id": sensor_id,
            "series": series,
            "status_counts": statuses,
            "audit_records": [
                point for point in points
                if point.get("fault_type") != "healthy"
            ],
            "repair_timing": "The offline repair pipeline ran after all 60 recorded seconds were accumulated.",
        }
        yield f"event: cycle_complete\ndata: {json.dumps(safe_value(completed), allow_nan=False)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/runs")
@app.post("/api/run-demo")
def api_run_demo(request: RunRequest = RunRequest(), user: dict = Depends(current_user)) -> dict:
    run = create_run_record(request.name, request.partition, user["id"])
    return {"status": "ok", "run": run}


@app.post("/jobs", status_code=202)
def create_partition_job(
    request: RunRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(current_user),
) -> dict:
    job_id = str(uuid.uuid4())
    create_job(job_id, user["id"])
    if os.getenv("HEALPIPE_TASK_MODE", "inline").lower() == "celery":
        from backend.tasks import process_partition_task

        process_partition_task.delay(job_id, user["id"], request.model_dump())
    else:
        background_tasks.add_task(_process_partition_job, job_id, user["id"], request)
    return {"job_id": job_id, "status": "queued"}


@app.post("/jobs/upload", status_code=202)
async def upload_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user: dict | None = Depends(optional_user),
) -> dict:
    if user is None and os.getenv("DEMO_MODE") != "1":
        raise HTTPException(status_code=401, detail="Authentication is required for telemetry uploads.")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".csv", ".parquet"}:
        raise HTTPException(status_code=415, detail="Only CSV and Parquet telemetry files are accepted.")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Upload exceeds the 25 MB size limit.")
    job_id = str(uuid.uuid4())
    path = UPLOADS / f"{job_id}{suffix}"
    path.write_bytes(content)
    owner_id = user["id"] if user else None
    create_job(job_id, owner_id, str(path))
    upload_name = Path(file.filename or "telemetry").stem[:100]
    if os.getenv("HEALPIPE_TASK_MODE", "inline").lower() == "celery":
        from backend.tasks import process_upload_task

        process_upload_task.delay(job_id, owner_id, upload_name, str(path))
    else:
        background_tasks.add_task(
            _process_upload_job,
            job_id,
            owner_id,
            upload_name,
            path,
        )
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def job_status(job_id: str, user: dict | None = Depends(optional_user)) -> dict:
    if user is None and os.getenv("DEMO_MODE") != "1":
        raise HTTPException(status_code=401, detail="Authentication is required to inspect jobs.")
    job = get_job(job_id, user["id"] if user else None)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {
        key: job[key]
        for key in ("id", "status", "run_id", "error", "created_at", "updated_at")
    }


@app.get("/runs/{run_id}/series")
def run_series(
    run_id: int,
    sensor_id: str = Query(default="PS1"),
    cycle_id: int | None = Query(default=None, ge=0),
    user: dict = Depends(current_user),
) -> dict:
    run = get_run(run_id, user["id"])
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    if sensor_id not in SENSORS:
        raise HTTPException(status_code=422, detail=f"sensor_id must be one of {SENSORS}.")
    series = run["series"]
    if "sensor" in series and series["sensor"] == sensor_id:
        return {"run_id": run_id, **series}
    if sensor_id not in series:
        raise HTTPException(status_code=404, detail=f"No series was stored for {sensor_id}.")
    try:
        selected_series = select_run_series_cycle(series[sensor_id], cycle_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"run_id": run_id, **selected_series}


@app.get("/runs/{run_id}/audit")
def run_audit(run_id: int, user: dict = Depends(current_user)) -> Response:
    run = get_run(run_id, user["id"])
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    content = "\n".join(json.dumps(event, allow_nan=False) for event in run["events"])
    return Response(content=f"{content}\n" if content else "", media_type="application/x-ndjson")


@app.get("/runs/{run_id}/rig")
def run_rig(
    run_id: int,
    cycle_id: int = Query(ge=0),
    user: dict = Depends(current_user),
) -> dict:
    run = get_run(run_id, user["id"])
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    cycle_events = [
        event for event in run["events"]
        if event.get("cycle_id") is not None and int(event["cycle_id"]) == cycle_id
    ]
    if not cycle_events:
        raise HTTPException(status_code=404, detail=f"No audit records exist for cycle {cycle_id}.")
    return {
        "cycle_id": cycle_id,
        "sensor_map": load_sensor_map(),
        "sensors": {
            sensor: [event for event in cycle_events if event.get("sensor_id") == sensor]
            for sensor in SENSORS
        },
    }
