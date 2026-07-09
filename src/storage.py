"""Persistencia en SQLite + guardado de frames de evidencia."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from src.decision import FinalVerdict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at DATETIME,
    ended_at DATETIME,
    profile_name TEXT,
    total_inspections INTEGER,
    pass_count INTEGER,
    fail_count INTEGER,
    warn_count INTEGER
);

CREATE TABLE IF NOT EXISTS inspections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id),
    timestamp DATETIME,
    verdict TEXT,
    overall_confidence REAL,
    summary TEXT,
    defects_json TEXT,
    latency_ms INTEGER,
    frame_path TEXT,
    raw_response TEXT,
    model TEXT,
    primary_response TEXT
);
"""


class Storage:
    def __init__(self, db_path: str, sessions_dir: str, storage_settings: dict | None = None):
        """Abre (o crea) la DB SQLite, aplica el esquema/migraciones y prepara los directorios de sesión."""
        self.db_path = Path(db_path)
        self.sessions_dir = Path(sessions_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        cfg = storage_settings or {}
        self._save_frames = {
            "PASS": cfg.get("save_pass_frames", False),
            "WARN": cfg.get("save_warn_frames", True),
            "FAIL": cfg.get("save_fail_frames", True),
        }

        # El AnalysisWorker escribe desde otro thread: conexión compartida + lock
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Agrega columnas nuevas a DBs creadas por versiones anteriores."""
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(inspections)")}
        if "raw_response" not in cols:
            self._conn.execute("ALTER TABLE inspections ADD COLUMN raw_response TEXT")
        if "model" not in cols:
            self._conn.execute("ALTER TABLE inspections ADD COLUMN model TEXT")
        # primary_response: raw del modelo primario cuando hubo escalado híbrido
        if "primary_response" not in cols:
            self._conn.execute("ALTER TABLE inspections ADD COLUMN primary_response TEXT")

    def start_session(self, profile_name: str) -> int:
        """Inserta una nueva fila en `sessions` y crea el directorio de frames de la sesión."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (started_at, profile_name, total_inspections,"
                " pass_count, fail_count, warn_count) VALUES (?, ?, 0, 0, 0, 0)",
                (datetime.now().isoformat(), profile_name),
            )
            self._conn.commit()
            session_id = cur.lastrowid
        (self.sessions_dir / str(session_id) / "frames").mkdir(parents=True, exist_ok=True)
        return session_id

    def save_inspection(
        self,
        session_id: int,
        verdict: FinalVerdict,
        result,  # InspectionResult
        frame: np.ndarray | None = None,
    ) -> int:
        """Guarda una inspección en `inspections` (y el frame en disco, según config) y actualiza contadores de la sesión."""
        frame_path = ""
        if frame is not None and self._save_frames.get(verdict.status, False):
            frame_path = str(
                self.sessions_dir
                / str(session_id)
                / "frames"
                / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{verdict.status}.jpg"
            )
            cv2.imwrite(frame_path, frame)

        defects_json = json.dumps(
            [asdict(d) for d in result.defects], ensure_ascii=False
        )
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO inspections (session_id, timestamp, verdict,"
                " overall_confidence, summary, defects_json, latency_ms, frame_path,"
                " raw_response, model, primary_response)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    result.timestamp.isoformat(),
                    verdict.status,
                    result.overall_confidence,
                    result.summary,
                    defects_json,
                    result.latency_ms,
                    frame_path,
                    result.raw_response,
                    getattr(result, "model", ""),
                    getattr(result, "primary_response", "")
                    or getattr(result, "primary_raw_response", ""),
                ),
            )
            col = {"PASS": "pass_count", "WARN": "warn_count", "FAIL": "fail_count"}[
                verdict.status
            ]
            self._conn.execute(
                f"UPDATE sessions SET total_inspections = total_inspections + 1,"
                f" {col} = {col} + 1 WHERE id = ?",
                (session_id,),
            )
            self._conn.commit()
            return cur.lastrowid

    def end_session(self, session_id: int) -> None:
        """Marca la sesión como finalizada con el timestamp actual."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (datetime.now().isoformat(), session_id),
            )
            self._conn.commit()

    def get_session_stats(self, session_id: int) -> dict:
        """Retorna los datos de la sesión más porcentajes PASS/WARN/FAIL y latencia promedio."""
        with self._lock:
            session = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise ValueError(f"Sesión {session_id} no existe")
            avg = self._conn.execute(
                "SELECT AVG(latency_ms) AS avg_latency FROM inspections WHERE session_id = ?",
                (session_id,),
            ).fetchone()

        stats = dict(session)
        total = stats["total_inspections"] or 0
        for key in ("pass", "fail", "warn"):
            count = stats[f"{key}_count"] or 0
            stats[f"{key}_pct"] = (count / total * 100) if total else 0.0
        stats["avg_latency_ms"] = int(avg["avg_latency"] or 0)
        return stats

    def get_inspections(self, session_id: int) -> list[dict]:
        """Retorna todas las inspecciones de la sesión, ordenadas por timestamp, con defectos deserializados."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM inspections WHERE session_id = ? ORDER BY timestamp",
                (session_id,),
            ).fetchall()
        inspections = []
        for row in rows:
            item = dict(row)
            item["defects"] = json.loads(item.get("defects_json") or "[]")
            inspections.append(item)
        return inspections

    def get_last_session_id(self) -> int | None:
        """Retorna el id de la sesión más reciente, o None si no hay ninguna."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["id"] if row else None

    def close(self) -> None:
        """Cierra la conexión SQLite."""
        with self._lock:
            self._conn.close()
