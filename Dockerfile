FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HEALPIPE_ENV=production \
    HEALPIPE_TASK_MODE=celery \
    HEALPIPE_DB_PATH=/data/healpipe.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p Data/splits Data/corrupted artifacts/models artifacts/calibration /data
COPY app.py project_paths.py ./
COPY backend ./backend
COPY pipeline ./pipeline
COPY templates ./templates
COPY static ./static
COPY artifacts/models ./artifacts/models
COPY artifacts/calibration/confidence.joblib ./artifacts/calibration/confidence.joblib
COPY artifacts/reference_scales.json artifacts/residual_reference.json ./artifacts/
COPY Data/splits/train_reference.parquet Data/splits/validation_reference.parquet Data/splits/test_reference.parquet ./Data/splits/
COPY Data/corrupted/validation_corrupted.parquet Data/corrupted/test_corrupted.parquet ./Data/corrupted/
COPY artifacts/evaluation ./artifacts/evaluation

EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
