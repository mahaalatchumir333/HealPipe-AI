# HealPipe AI

HealPipe AI aligns hydraulic telemetry, detects missing/frozen/spiking/drifting sensor values, reconstructs them from local interpolation or cross-sensor models, and records a diagnosis, confidence, and point status.

## Local setup

Use Python 3.12 or 3.13. From the project root in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

The checked-in `Data/` directory is expected for a local run. If rebuilding the dataset, put the extracted UCI/Kaggle sensor matrices in `Data/raw/`, then run:

```powershell
.\.venv\Scripts\python.exe prepare_data.py
.\.venv\Scripts\python.exe inject.py --partition validation --seed 101
.\.venv\Scripts\python.exe inject.py --partition test --seed 202
```

## Train and evaluate

Cross-fit by complete training cycles, save the final per-sensor models and residual distributions, then train confidence calibration from the validation split:

```powershell
.\.venv\Scripts\python.exe -m pipeline.train_models --folds 5
.\.venv\Scripts\python.exe -m pipeline.calibrate
.\.venv\Scripts\python.exe -m pipeline.evaluate
.\.venv\Scripts\python.exe -m pytest tests -q
```

The evaluation writes `artifacts/evaluation/test_evaluation.json`; synthetic labels are used only by evaluation and calibration, never passed to the detector.

## Validation tuning and benchmark

Detector parameter search reads validation cycles only. It writes the selected versioned parameters and validation stratification report; calibrate confidence after changing detector parameters. The original held-out report remains the pre-tuning baseline. Do not select settings from test errors or labels.

```powershell
.\.venv\Scripts\python.exe -m pipeline.tune_detector --search-cycles 30 --seed 1701
.\.venv\Scripts\python.exe -m pipeline.calibrate
```

The completed one-time seed-303 test evaluation is stored separately from the original report. To reproduce the separate files (do not use the test result to tune):

```powershell
.\.venv\Scripts\python.exe inject.py --partition test --seed 303 --output-name test_v2
.\.venv\Scripts\python.exe -m pipeline.evaluate --partition test_v2
```

Generate the validation-only benchmark as CSV and JSON. The CLI prints a runtime estimate; `--max-cells` limits a pilot run. The full grid includes one-second freeze cells as inherently ambiguous cases and omits unsupported affected-channel counts (the configured group has one power channel and two flow channels).

```powershell
.\.venv\Scripts\python.exe -m pipeline.benchmark --repeats 3 --seed 2601
.\.venv\Scripts\python.exe -m pipeline.benchmark --repeats 1 --max-cells 4 --seed 2601
```

The dashboard's Benchmark view reads the generated report. Re-running a bounded pilot replaces the files in `artifacts/benchmark/`; run the complete command to restore the full report.

## Schema and recorded replay

`artifacts/sensor_schema.json` configures enabled channels and source rates; all processing remains on a 1 Hz grid (one-second averages). The optional physical channels PS4-PS6 and VS1 are disabled because using them requires retraining and separate evaluation. CE, CP, and SE are derived/virtual channels and are excluded from supporting inputs. No optional-channel training results are claimed.

The dashboard's **Live telemetry (replay)** streams a recorded corrupted 60-second cycle with a clearly labelled SIMULATED clock, then runs the existing offline pipeline at cycle completion. It does not repair each second as it arrives. `RealTelemetryAdapter` defines a schema-checked interface for a future real feed; this repository does not fabricate a live feed.

The offline schematic uses vendored Three.js with a 2D fallback. It is an illustrative animated layout, not a CAD model and not to scale. Sensor-map locations are marked “not documented” where the dataset documentation gives no individual position. Displayed cooler, valve, pump, and accumulator values are cycle context only, not a component diagnosis.

## Run the website

For development, open two terminals:

```powershell
.\.venv\Scripts\python.exe app.py
```

Then open http://127.0.0.1:8000/dashboard for the read-only evaluation dashboard, or http://127.0.0.1:8000 for the operational workflow overview. Evaluation aggregates and corrupted/repaired demo traces are available without signing in; displaying hidden reference values remains authenticated. User-specific runs, uploads, and audit exports remain protected API operations.

The OpenAPI page is available at http://127.0.0.1:8000/docs. Protected APIs require an `Authorization: Bearer <token>` header. Test/evaluation truth labels are not exposed by the web API.

When `HEALPIPE_JWT_SECRET` is not set in development, the app creates a local `.healpipe-dev-secret` file so tokens remain valid across server restarts. Keep this file private and do not copy it into deployments; production requires a managed `HEALPIPE_JWT_SECRET`.

The evaluation dashboard defaults to showing corrupted and repaired values only. An authenticated client can enable “Show hidden reference (judge mode)” to request and overlay the held-out reference trace for a demonstration. Choose a complete 60-second cycle from the cycle selector to inspect point-level diagnoses, confidence scores, acceptance/refusal decisions, and abstentions.

Use the deterministic fault-scenario lab to vary fault type, duration, severity, starting sensor, and one to three affected channels. The scenario passes corrupted telemetry—not reference values—to the detector and reports diagnosis precision/recall/F1, false alarms, confidence, accepted repairs, refusals, status counts, and accepted-repair success within tolerance. The dashboard overlays the selected sensor's observed and repaired values and lists point-level scenario decisions; repeat parameters to reproduce the same result. This is a demonstration/evaluation tool, not random prediction variation. Persisted run records and JSONL audit exports remain available through authenticated run APIs.

Each scenario-lab result is persisted with its parameters, point-level audit decisions, series, and summary metrics. The dashboard can download the saved result as JSONL. The CSV/Parquet upload control is available to anonymous local users only when `DEMO_MODE=1`; otherwise authentication is required. Uploads are limited to 25 MB, only CSV/Parquet are accepted, and files must contain exactly the configured telemetry columns with numeric values and complete, unique 60-second cycles. Pickle/joblib uploads are not accepted. For local demo mode in PowerShell:

```powershell
$env:DEMO_MODE = "1"
.\.venv\Scripts\python.exe app.py
```

Unset the flag after the local demo with `Remove-Item Env:DEMO_MODE`.

All injected faults in this repository are synthetic and statistically scaled. A 1 Hz value is a one-second average, not a raw 100 Hz reading. Repair tolerance is a statistical proxy (1.5 times the robust training reference scale), not an equipment specification. Replay time is simulated, not acquisition time. The project does not claim production readiness, financial savings, fault diagnosis of physical components, or field reliability.

## Docker deployment

For local Docker development, set secrets and expose the API port:

```powershell
$env:HEALPIPE_JWT_SECRET = [guid]::NewGuid().ToString() + [guid]::NewGuid().ToString()
$env:REDIS_PASSWORD = [guid]::NewGuid().ToString()
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

For a TLS-terminated deployment, provide a certificate and private key and start the HTTPS proxy:

```powershell
$env:TLS_CERT_PATH = "C:\path\to\fullchain.pem"
$env:TLS_KEY_PATH = "C:\path\to\privkey.pem"
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```

Production secrets and certificate paths must be supplied from a secure secret manager in the hosting environment. Back up the SQLite volume and uploads volume. The demo uses SQLite and local volumes; production multi-instance installations should move to PostgreSQL and encrypted object storage. TLS, encrypted disks/volumes, access policies, retention, backups, and privacy/legal reviews must be configured for the chosen host before handling real sensitive or regulated data. The project does not claim HIPAA or GDPR certification.

## Current service scope

- FastAPI serves the static dashboard, bearer JWT authentication, TOTP MFA enrollment, run history, point-level JSONL audit output, and CSV/Parquet upload.
- SQLite stores users, jobs, and run records. Redis/Celery is used in the Docker configuration; local development uses in-process background tasks.
- The frontend is a responsive static HTML/CSS/JavaScript UI, not a React/Vite build.
- Password recovery, rate-limit policy tuned for a deployment, MFA recovery codes, user administration, cloud storage/CDN, and hosted CI/CD still require deployment-specific decisions.
