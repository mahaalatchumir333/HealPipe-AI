import base64
import hashlib
import hmac
import io
import json
import struct
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend import database
from backend.main import app
from backend.validation import validate_telemetry
from pipeline.reconstruct import SENSORS


def _totp(secret: str) -> str:
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    counter = int(time.time() // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    with TestClient(app) as test_client:
        yield test_client


def _register_and_login(client, username):
    password = "correct horse battery staple 42"
    response = client.post(
        "/auth/register",
        json={"username": username, "password": password},
    )
    assert response.status_code == 201
    response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return password, response.json()["access_token"]


def test_auth_mfa_and_run_access_is_account_scoped(client, monkeypatch):
    password, token = _register_and_login(client, "pipeline-user")
    auth = {"Authorization": f"Bearer {token}"}
    setup = client.post("/auth/mfa/setup", headers=auth)
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    confirm = client.post(
        "/auth/mfa/confirm",
        headers=auth,
        json={"code": _totp(secret)},
    )
    assert confirm.status_code == 200

    assert client.post(
        "/auth/login",
        json={"username": "pipeline-user", "password": password},
    ).status_code == 401
    login = client.post(
        "/auth/login",
        json={
            "username": "pipeline-user",
            "password": password,
            "mfa_code": _totp(secret),
        },
    )
    assert login.status_code == 200
    first_token = login.json()["access_token"]
    first_user = client.get("/api/runs", headers={"Authorization": f"Bearer {first_token}"})
    assert first_user.status_code == 200

    _, second_token = _register_and_login(client, "another-user")

    def fake_run(name, partition, owner_id):
        run_id = database.insert_run(
            name,
            partition,
            "completed",
            {"modified_count": 1},
            [{
                "cycle_id": 1,
                "second_in_cycle": 1,
                "sensor_id": "PS1",
                "observed_value": 2.0,
                "reconstructed_value": 1.0,
                "fault_type": "spike",
                "status": "Corrected",
                "confidence": 94.0,
                "diagnosis": "Isolated outlier.",
            }],
            {"PS1": {
                "sensor": "PS1",
                "observed": [2.0],
                "repaired": [1.0],
                "faults": ["spike"],
                "confidence": [94.0],
                "points": [{
                    "cycle_id": 1,
                    "second_in_cycle": 1,
                    "sensor_id": "PS1",
                    "observed_value": 2.0,
                    "reconstructed_value": 1.0,
                    "fault_type": "spike",
                    "status": "Corrected",
                    "confidence": 94.0,
                }],
            }},
            owner_id,
        )
        return {"run_id": run_id, "name": name, "partition": partition, "status": "completed"}

    monkeypatch.setattr("backend.main.create_run_record", fake_run)
    created = client.post(
        "/runs",
        headers={"Authorization": f"Bearer {first_token}"},
        json={"name": "private-run", "partition": "validation"},
    )
    assert created.status_code == 200
    run_id = created.json()["run"]["run_id"]
    series = client.get(
        f"/runs/{run_id}/series?sensor_id=PS1",
        headers={"Authorization": f"Bearer {first_token}"},
    )
    assert series.status_code == 200
    assert series.json()["observed"] == [2.0]
    assert series.json()["selected_cycle"] == 1
    assert client.get(
        f"/runs/{run_id}/series?sensor_id=PS1&cycle_id=999",
        headers=series.request.headers,
    ).status_code == 422
    assert client.get(
        f"/runs/{run_id}/series?sensor_id=UNKNOWN",
        headers={"Authorization": f"Bearer {first_token}"},
    ).status_code == 422
    assert client.get(
        f"/runs/{run_id}/audit",
        headers={"Authorization": f"Bearer {second_token}"},
    ).status_code == 404
    audit = client.get(
        f"/runs/{run_id}/audit",
        headers={"Authorization": f"Bearer {first_token}"},
    )
    assert audit.status_code == 200
    assert audit.headers["content-type"].startswith("application/x-ndjson")
    assert '"fault_type": "spike"' in audit.text


def test_telemetry_upload_validation():
    frame = pd.DataFrame({
        "cycle_id": [1] * 60,
        "second_in_cycle": list(range(60)),
        **{sensor: [1.0] * 60 for sensor in SENSORS},
    })
    assert len(validate_telemetry(frame)) == 60
    with pytest.raises(Exception):
        validate_telemetry(frame.drop(columns=["PS1"]))
    with pytest.raises(Exception):
        validate_telemetry(frame.iloc[:-1])
    duplicate = pd.concat([frame, frame], ignore_index=True)
    with pytest.raises(Exception):
        validate_telemetry(duplicate)
    invalid_numeric = frame.copy()
    invalid_numeric["PS1"] = invalid_numeric["PS1"].astype(object)
    invalid_numeric.loc[0, "PS1"] = "not-a-number"
    with pytest.raises(Exception, match="PS1 must contain numeric values"):
        validate_telemetry(invalid_numeric)


def test_evaluation_is_read_only_public_and_reference_values_remain_protected(client):
    report = client.get("/api/evaluation?partition=test")
    assert report.status_code == 200
    payload = report.json()
    assert payload["partition"] == "test"
    assert set(payload["fault_distribution"]).issuperset({"missing", "freeze", "spike", "drift"})
    assert "injected_fault" not in payload

    series = client.get(
        "/api/evaluation/series?partition=test&sensor_id=PS1",
    )
    assert series.status_code == 200
    series_payload = series.json()
    assert len(series_payload["observed"]) == 60
    assert "reference" not in series_payload
    assert "reference_value" not in series_payload["points"][0]
    assert "injected_fault" not in series.json()
    assert series_payload["points"][0]["acceptance_decision"] in {"accepted", "refused", "retained"}
    assert series_payload["points"][0]["confidence_method"] == "validation_calibrated_logistic_regression"
    stale_auth_series = client.get(
        "/api/evaluation/series?partition=test&sensor_id=PS1&include_reference=false",
        headers={"Authorization": "Bearer expired-or-invalid-token"},
    )
    assert stale_auth_series.status_code == 200
    assert client.get(
        "/api/evaluation/series?partition=test&sensor_id=PS1&include_reference=true",
    ).status_code == 401
    assert client.get(
        "/api/evaluation/series?partition=test&sensor_id=PS1&include_reference=true",
        headers={"Authorization": "Bearer expired-or-invalid-token"},
    ).status_code == 401

    _, token = _register_and_login(client, "evaluator")
    headers = {"Authorization": f"Bearer {token}"}
    reference_series = client.get(
        "/api/evaluation/series?partition=test&sensor_id=PS1&include_reference=true",
        headers=headers,
    )
    assert reference_series.status_code == 200
    assert len(reference_series.json()["reference"]) == 60
    assert "injected_fault" not in reference_series.json()
    invalid_cycle = client.get(
        "/api/evaluation/series?partition=test&sensor_id=PS1&cycle_id=999999",
        headers=headers,
    )
    assert invalid_cycle.status_code == 422


def test_public_rig_endpoint_exposes_sensor_metadata_and_cycle_decisions(client, monkeypatch):
    expected = {
        "partition": "validation",
        "cycle_id": 1,
        "sensor_map": {"sensors": {"PS1": {"location_area": "not documented"}}},
        "sensors": {"PS1": [{"sensor_id": "PS1", "status": "Measured"}]},
    }
    monkeypatch.setattr("backend.main.evaluation_rig", lambda partition, cycle_id: expected)

    response = client.get("/api/evaluation/rig?partition=validation&cycle_id=1")

    assert response.status_code == 200
    assert response.json() == expected


def test_scenario_api_runs_repeatable_faults_and_evaluates_full_decision(client):
    request = {
        "partition": "validation",
        "sensor_id": "PS1",
        "fault_type": "missing",
        "start_second": 20,
        "duration_seconds": 5,
        "severity": 6,
        "affected_channels": 2,
    }
    first = client.post("/api/scenarios/evaluate", json=request)
    second = client.post("/api/scenarios/evaluate", json=request)
    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["scenario"] == second_payload["scenario"]
    assert first_payload["metrics"] == second_payload["metrics"]
    assert first_payload["series"] == second_payload["series"]
    assert first_payload["run_id"] != second_payload["run_id"]
    saved_run = database.get_run(first_payload["run_id"])
    assert saved_run is not None
    assert saved_run["metrics"]["scenario_parameters"] == first_payload["scenario"]
    assert saved_run["events"] == first_payload["fault_audit"]
    fetched = client.get(f"/api/scenarios/runs/{first_payload['run_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["scenario"] == first_payload["scenario"]
    assert fetched.json()["metrics"] == first_payload["metrics"]
    assert fetched.json()["fault_audit"] == first_payload["fault_audit"]
    download = client.get(
        f"/api/scenarios/runs/{first_payload['run_id']}/audit"
    )
    assert download.status_code == 200
    lines = download.text.splitlines()
    assert lines
    parsed_events = [
        json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        for line in lines
    ]
    assert parsed_events == first_payload["fault_audit"]
    assert download.headers["content-type"].startswith("application/x-ndjson")
    assert all("NaN" not in line and "Infinity" not in line for line in lines)
    assert first_payload["scenario"]["deterministic"] is True
    assert first_payload["fault_audit"][0]["location_area"] == "not documented"
    assert first_payload["fault_audit"][0]["component_context"]["context_only"] is True
    assert "not a diagnosis of a failed component" in first_payload["fault_audit"][0]["location_statement"]
    assert len(first_payload["scenario"]["sensors"]) == 2
    assert first_payload["metrics"]["injected_points"] == 10
    assert {
        "diagnosis_precision",
        "diagnosis_recall",
        "diagnosis_f1",
        "accepted_repairs",
        "refused_repairs",
        "accepted_repair_success_rate",
        "false_alarms_outside_injected_region",
        "status_counts",
    }.issubset(first_payload["metrics"])
    assert len(first_payload["series"]["points"]) == 60
    assert len(first_payload["fault_audit"]) == 10
    assert "reference_value" not in first_payload["series"]["points"][0]
    assert "injected_fault" not in first_payload["series"]["points"][0]
    changed = client.post(
        "/api/scenarios/evaluate",
        json={
            **request,
            "fault_type": "spike",
            "duration_seconds": 1,
            "severity": 10,
            "affected_channels": 1,
        },
    )
    assert changed.status_code == 200
    changed_payload = changed.json()
    assert changed_payload["scenario"]["fault_type"] == "spike"
    assert changed_payload["scenario"]["severity_sensor_scales"] == 10
    assert changed_payload["series"]["observed"][20] != first_payload["series"]["observed"][20]
    assert client.post(
        "/api/scenarios/evaluate",
        json={**request, "fault_type": "freeze", "duration_seconds": 2},
    ).status_code == 422


def test_async_run_and_upload_jobs_require_auth_and_finish(client, monkeypatch, tmp_path):
    from backend import main

    upload_directory = tmp_path / "uploads"
    upload_directory.mkdir()
    monkeypatch.setattr(main, "UPLOADS", upload_directory)
    assert client.post("/jobs", json={"name": "blocked"}).status_code == 401
    _, token = _register_and_login(client, "job-user")
    auth = {"Authorization": f"Bearer {token}"}

    def finish_partition(job_id, owner_id, request):
        run_id = database.insert_run(
            request.name,
            request.partition,
            "completed",
            {},
            [],
            {},
            owner_id,
        )
        database.update_job(job_id, "completed", run_id=run_id)

    def finish_upload(job_id, owner_id, name, path):
        run_id = database.insert_run(name, "upload", "completed", {}, [], {}, owner_id)
        database.update_job(job_id, "completed", run_id=run_id)

    monkeypatch.setattr("backend.main._process_partition_job", finish_partition)
    monkeypatch.setattr("backend.main._process_upload_job", finish_upload)

    queued = client.post(
        "/jobs",
        headers=auth,
        json={"name": "queued-run", "partition": "validation"},
    )
    assert queued.status_code == 202
    job_id = queued.json()["job_id"]
    assert client.get(f"/jobs/{job_id}", headers=auth).json()["status"] == "completed"

    rejected = client.post(
        "/jobs/upload",
        headers=auth,
        files={"file": ("telemetry.txt", b"not supported", "text/plain")},
    )
    assert rejected.status_code == 415

    csv_frame = pd.DataFrame({
        "cycle_id": [1] * 60,
        "second_in_cycle": list(range(60)),
        **{sensor: [1.0] * 60 for sensor in SENSORS},
    })
    csv_content = csv_frame.to_csv(index=False).encode()
    uploaded = client.post(
        "/jobs/upload",
        headers=auth,
        files={"file": ("telemetry.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert uploaded.status_code == 202
    upload_job_id = uploaded.json()["job_id"]
    assert client.get(f"/jobs/{upload_job_id}", headers=auth).json()["status"] == "completed"


def test_demo_upload_requires_flag_and_allows_validated_anonymous_upload(client, monkeypatch, tmp_path):
    from backend import main

    upload_directory = tmp_path / "demo-uploads"
    upload_directory.mkdir()
    monkeypatch.setattr(main, "UPLOADS", upload_directory)
    monkeypatch.delenv("DEMO_MODE", raising=False)
    csv_frame = pd.DataFrame({
        "cycle_id": [7] * 60,
        "second_in_cycle": list(range(60)),
        **{sensor: [2.0] * 60 for sensor in SENSORS},
    })
    file_data = csv_frame.to_csv(index=False).encode()
    assert client.post(
        "/jobs/upload",
        files={"file": ("telemetry.csv", file_data, "text/csv")},
    ).status_code == 401

    monkeypatch.setenv("DEMO_MODE", "1")
    assert client.post(
        "/jobs/upload",
        files={"file": ("telemetry.pkl", b"pickle", "application/octet-stream")},
    ).status_code == 415
    from backend import main
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 4)
    assert client.post(
        "/jobs/upload",
        files={"file": ("too-large.csv", b"12345", "text/csv")},
    ).status_code == 413
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 25 * 1024 * 1024)

    def finish_upload(job_id, owner_id, name, path):
        database.update_job(job_id, "completed")

    monkeypatch.setattr("backend.main._process_upload_job", finish_upload)
    uploaded = client.post(
        "/jobs/upload",
        files={"file": ("telemetry.csv", file_data, "text/csv")},
    )
    assert uploaded.status_code == 202
    job_id = uploaded.json()["job_id"]
    assert client.get(f"/jobs/{job_id}").json()["status"] == "completed"
    assert client.get("/api/config").json()["demo_upload_enabled"] is True
