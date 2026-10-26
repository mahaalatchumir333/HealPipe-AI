import base64
import hashlib
import hmac
import io
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
        "cycle_id": [1],
        "second_in_cycle": [0],
        **{sensor: [1.0] for sensor in SENSORS},
    })
    assert len(validate_telemetry(frame)) == 1
    with pytest.raises(Exception):
        validate_telemetry(frame.drop(columns=["PS1"]))
    duplicate = pd.concat([frame, frame], ignore_index=True)
    with pytest.raises(Exception):
        validate_telemetry(duplicate)


def test_evaluation_endpoints_require_auth_and_do_not_return_private_labels(client):
    assert client.get("/api/evaluation").status_code == 401
    _, token = _register_and_login(client, "evaluator")
    headers = {"Authorization": f"Bearer {token}"}
    report = client.get("/api/evaluation?partition=test", headers=headers)
    assert report.status_code == 200
    payload = report.json()
    assert payload["partition"] == "test"
    assert set(payload["fault_distribution"]).issuperset({"missing", "freeze", "spike", "drift"})
    assert "injected_fault" not in payload

    series = client.get(
        "/api/evaluation/series?partition=test&sensor_id=PS1",
        headers=headers,
    )
    assert series.status_code == 200
    series_payload = series.json()
    assert len(series_payload["observed"]) == 60
    assert "reference" not in series_payload
    assert "reference_value" not in series_payload["points"][0]
    assert "injected_fault" not in series.json()
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

    sensors = ",".join(SENSORS)
    row = ",".join(["1", "0", *["1.0"] * len(SENSORS)])
    csv_content = f"cycle_id,second_in_cycle,{sensors}\n{row}\n".encode()
    uploaded = client.post(
        "/jobs/upload",
        headers=auth,
        files={"file": ("telemetry.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert uploaded.status_code == 202
    upload_job_id = uploaded.json()["job_id"]
    assert client.get(f"/jobs/{upload_job_id}", headers=auth).json()["status"] == "completed"
