FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HEALPIPE_ENV=production \
    HEALPIPE_TASK_MODE=inline \
    HEALPIPE_DB_PATH=/data/healpipe.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /data Data/uploads

COPY app.py project_paths.py ./
COPY backend ./backend
COPY pipeline ./pipeline
COPY templates ./templates
COPY static ./static

COPY artifacts ./artifacts

COPY Data/splits ./Data/splits
COPY Data/corrupted ./Data/corrupted
COPY Data/reference/profile_metadata.parquet ./Data/reference/profile_metadata.parquet

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]