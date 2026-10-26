from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("HEALPIPE_DB_PATH", str(ROOT / "healpipe.db")))


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                partition TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_id INTEGER,
                metrics TEXT NOT NULL,
                events TEXT NOT NULL DEFAULT '[]',
                series TEXT NOT NULL DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(owner_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                mfa_secret TEXT,
                pending_mfa_secret TEXT,
                mfa_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                owner_id INTEGER,
                status TEXT NOT NULL,
                upload_path TEXT,
                run_id INTEGER,
                error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(owner_id) REFERENCES users(id),
                FOREIGN KEY(run_id) REFERENCES runs(id)
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        if "owner_id" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN owner_id INTEGER")
        if "events" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN events TEXT NOT NULL DEFAULT '[]'")
        if "series" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN series TEXT NOT NULL DEFAULT '{}'")
        conn.commit()
    finally:
        conn.close()


def insert_run(
    name: str,
    partition: str,
    status: str,
    metrics: dict[str, Any],
    events: list[dict[str, Any]],
    series: dict[str, Any],
    owner_id: int | None = None,
) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO runs (name, partition, status, owner_id, metrics, events, series) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                partition,
                status,
                owner_id,
                json.dumps(metrics, allow_nan=False),
                json.dumps(events, allow_nan=False),
                json.dumps(series, allow_nan=False),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def list_runs(owner_id: int | None = None) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        if owner_id is None:
            rows = conn.execute(
                "SELECT id, name, partition, status, metrics, events, created_at FROM runs WHERE owner_id IS NULL ORDER BY id DESC LIMIT 12"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, partition, status, metrics, events, created_at FROM runs WHERE owner_id = ? ORDER BY id DESC LIMIT 12",
                (owner_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "metrics": json.loads(row["metrics"]),
                "events": json.loads(row["events"]),
            }
            for row in rows
        ]
    finally:
        conn.close()


def get_run(run_id: int, owner_id: int | None = None) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, partition, status, metrics, events, series, created_at FROM runs WHERE id = ? AND owner_id IS ?",
            (run_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        return {
            **dict(row),
            "metrics": json.loads(row["metrics"]),
            "events": json.loads(row["events"]),
            "series": json.loads(row["series"]),
        }
    finally:
        conn.close()


def get_user(username: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, mfa_secret, pending_mfa_secret, mfa_enabled FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_user(username: str, password_hash: str) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hash),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def update_user_mfa(username: str, secret: str | None, enabled: bool, pending: str | None = None) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE users
            SET mfa_secret = COALESCE(?, mfa_secret),
                mfa_enabled = CASE WHEN ? = 1 THEN 1 ELSE mfa_enabled END,
                pending_mfa_secret = ?
            WHERE username = ?
            """,
            (secret, int(enabled), pending, username),
        )
        conn.commit()
    finally:
        conn.close()


def create_job(job_id: str, owner_id: int | None, upload_path: str | None = None) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO jobs (id, owner_id, status, upload_path) VALUES (?, ?, 'queued', ?)",
            (job_id, owner_id, upload_path),
        )
        conn.commit()
    finally:
        conn.close()


def update_job(job_id: str, status: str, run_id: int | None = None, error: str | None = None) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, run_id = COALESCE(?, run_id), error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, run_id, error, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_job(job_id: str, owner_id: int) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, owner_id, status, upload_path, run_id, error, created_at, updated_at FROM jobs WHERE id = ? AND owner_id = ?",
            (job_id, owner_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
