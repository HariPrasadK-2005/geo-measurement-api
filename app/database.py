"""Tiny persistence layer on top of the standard-library sqlite3 module.

Two tables:
  files    - one row per upload (status, CRS, feature count, error message)
  features - one row per feature (geometry, properties and measurement stored as JSON text)
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id            TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    status        TEXT NOT NULL,
    crs           TEXT,
    feature_count INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS features (
    file_id       TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,
    geometry_type TEXT,
    geometry      TEXT,
    crs           TEXT,
    properties    TEXT NOT NULL,
    measurement   TEXT NOT NULL,
    PRIMARY KEY (file_id, idx)
);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on error, always close."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)  # cheap: every statement is IF NOT EXISTS
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_file(file_id: str, filename: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO files (id, filename, status, created_at) VALUES (?, ?, 'PROCESSING', ?)",
            (file_id, filename, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def save_result(file_id: str, crs: str, features: list[dict[str, Any]]) -> None:
    """Store all features and mark the file COMPLETED in ONE transaction."""
    rows = [
        (
            file_id,
            f["index"],
            f["geometry_type"],
            json.dumps(f["geometry"]) if f["geometry"] is not None else None,
            f["crs"],
            json.dumps(f["properties"]),
            json.dumps(f["measurement"]),
        )
        for f in features
    ]
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO features (file_id, idx, geometry_type, geometry, crs, properties, measurement) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "UPDATE files SET status = 'COMPLETED', crs = ?, feature_count = ? WHERE id = ?",
            (crs, len(features), file_id),
        )


def mark_failed(file_id: str, error: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE files SET status = 'FAILED', error = ? WHERE id = ?", (error, file_id))


def get_file(file_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    return dict(row) if row else None


def get_features(file_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM features WHERE file_id = ? ORDER BY idx LIMIT ? OFFSET ?",
            (file_id, limit, offset),
        ).fetchall()
    return [
        {
            "index": r["idx"],
            "geometry_type": r["geometry_type"],
            "geometry": json.loads(r["geometry"]) if r["geometry"] else None,
            "crs": r["crs"],
            "properties": json.loads(r["properties"]),
            "measurement": json.loads(r["measurement"]),
        }
        for r in rows
    ]


def get_measurements(file_id: str) -> list[dict[str, Any]]:
    """Like get_features but skips the (potentially large) geometry column."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT idx, geometry_type, measurement FROM features WHERE file_id = ? ORDER BY idx",
            (file_id,),
        ).fetchall()
    return [
        {
            "index": r["idx"],
            "geometry_type": r["geometry_type"],
            "measurement": json.loads(r["measurement"]),
        }
        for r in rows
    ]
