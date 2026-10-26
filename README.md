# HealPipe AI

HealPipe AI aligns hydraulic telemetry, detects missing/frozen/spiking/drifting sensor values, reconstructs them from local interpolation or cross-sensor models, and records a diagnosis, confidence, and point status.

## Local setup

Use Python 3.12 or 3.13. From the project root in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

The checked-in `Data/` directory is expected for a local run. If rebuilding the dataset, put the extracted UCI/Kaggle sensor matrices in `Data/raw/`, then run:

```powershell
python prepare_data.py
python inject.py --partition validation --seed 101
python inject.py --partition test --seed 202
```

## Train and evaluate

Cross-fit by complete training cycles, save the final per-sensor models and residual distributions, then train confidence calibration from the validation split:

```powershell
python -m pipeline.train_models --folds 5
python -m pipeline.calibrate
python -m pipeline.evaluate
python -m pytest tests -q
```

The evaluation writes `artifacts/evaluation/test_evaluation.json`; synthetic labels are used only by evaluation and calibration, never passed to the detector.

## Run the website

For development, open two terminals:

```powershell
.\.venv\Scripts\Activate.ps1
python app.py
```

Then open http://127.0.0.1:8000. Register an account with a password of at least 12 characters, sign in, and optionally enable MFA from the account panel. Use “Run demo analysis” for the validation sample, or upload CSV/Parquet telemetry with `cycle_id`, `second_in_cycle`, and all ten sensors: `PS1`, `PS2`, `PS3`, `FS1`, `FS2`, `TS1`, `TS2`, `TS3`, `TS4`, `EPS1`. Uploads are limited to 25 MB and 100,000 rows.

The OpenAPI page is available at http://127.0.0.1:8000/docs. Protected APIs require an `Authorization: Bearer <token>` header. Test/evaluation truth labels are not exposed by the web API.

When `HEALPIPE_JWT_SECRET` is not set in development, the app creates a local `.healpipe-dev-secret` file so tokens remain valid across server restarts. Keep this file private and do not copy it into deployments; production requires a managed `HEALPIPE_JWT_SECRET`.

The authenticated evaluation dashboard defaults to showing corrupted and repaired values only. Enable “Show hidden reference (judge mode)” to request and overlay the held-out reference trace for a demonstration. Choose a complete 60-second cycle from the cycle selector to inspect point-level diagnoses and abstentions.

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
