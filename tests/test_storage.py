"""Tests de Storage: persistencia SQLite, guardado de frames y uso cross-thread."""

from __future__ import annotations

import threading

import numpy as np
import pytest

from src.analyzer import Defect, InspectionResult
from src.decision import FinalVerdict
from src.storage import Storage


def _result(verdict="PASS", confidence=0.9, defects=None, latency_ms=120):
    return InspectionResult(
        verdict=verdict,
        overall_confidence=confidence,
        defects=defects or [],
        summary=f"resumen {verdict}",
        evaluable=True,
        raw_response="{}",
        latency_ms=latency_ms,
    )


def _verdict(status="PASS", result=None):
    result = result or _result(status)
    return FinalVerdict(status=status, result=result, is_confirmed=True, consecutive_count=1)


@pytest.fixture
def storage(tmp_path):
    st = Storage(
        db_path=str(tmp_path / "qc.db"),
        sessions_dir=str(tmp_path / "sessions"),
    )
    yield st
    st.close()


def _frame():
    return np.zeros((4, 4, 3), dtype=np.uint8)


def test_start_session_creates_row_and_frames_dir(storage, tmp_path):
    sid = storage.start_session("pcb")
    assert isinstance(sid, int)
    assert (tmp_path / "sessions" / str(sid) / "frames").is_dir()
    assert storage.get_last_session_id() == sid


def test_save_inspection_updates_counts_and_stats(storage):
    sid = storage.start_session("generic")
    storage.save_inspection(sid, _verdict("PASS"), _result("PASS", latency_ms=100))
    storage.save_inspection(sid, _verdict("FAIL"), _result("FAIL", latency_ms=300))

    stats = storage.get_session_stats(sid)
    assert stats["total_inspections"] == 2
    assert stats["pass_count"] == 1
    assert stats["fail_count"] == 1
    assert stats["warn_count"] == 0
    assert stats["pass_pct"] == 50.0
    assert stats["avg_latency_ms"] == 200


def test_save_frames_respects_config(storage, tmp_path):
    sid = storage.start_session("generic")
    # PASS no se guarda por defecto; FAIL sí
    storage.save_inspection(sid, _verdict("PASS"), _result("PASS"), _frame())
    storage.save_inspection(sid, _verdict("FAIL"), _result("FAIL"), _frame())

    inspections = storage.get_inspections(sid)
    by_verdict = {i["verdict"]: i for i in inspections}
    assert by_verdict["PASS"]["frame_path"] == ""
    fail_path = by_verdict["FAIL"]["frame_path"]
    assert fail_path.endswith("_FAIL.jpg")
    assert (tmp_path / "sessions" / str(sid) / "frames").iterdir()


def test_get_inspections_parses_defects(storage):
    sid = storage.start_session("generic")
    defects = [Defect("Grieta", "major", "centro", 0.95)]
    storage.save_inspection(sid, _verdict("FAIL"), _result("FAIL", defects=defects))

    inspections = storage.get_inspections(sid)
    assert len(inspections) == 1
    parsed = inspections[0]["defects"]
    assert parsed[0]["severity"] == "major"
    assert parsed[0]["description"] == "Grieta"


def test_save_inspection_persists_raw_response(storage):
    """La respuesta cruda de Claude se guarda para auditoría (columna raw_response)."""
    sid = storage.start_session("generic")
    raw = '{"verdict": "FAIL", "summary": "Grieta"}'
    result = _result("FAIL")
    result.raw_response = raw
    storage.save_inspection(sid, _verdict("FAIL"), result)

    assert storage.get_inspections(sid)[0]["raw_response"] == raw


def test_migrate_adds_raw_response_to_legacy_db(tmp_path):
    """Una DB creada sin raw_response (versión previa) se migra sin perder datos."""
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE inspections (id INTEGER PRIMARY KEY, session_id INTEGER,"
        " timestamp DATETIME, verdict TEXT, overall_confidence REAL, summary TEXT,"
        " defects_json TEXT, latency_ms INTEGER, frame_path TEXT);"
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY, started_at DATETIME,"
        " ended_at DATETIME, profile_name TEXT, total_inspections INTEGER,"
        " pass_count INTEGER, fail_count INTEGER, warn_count INTEGER);"
    )
    conn.commit()
    conn.close()

    st = Storage(db_path=str(db), sessions_dir=str(tmp_path / "s"))
    try:
        sid = st.start_session("generic")
        st.save_inspection(sid, _verdict("PASS"), _result("PASS"))
        assert "raw_response" in st.get_inspections(sid)[0]
    finally:
        st.close()


def test_get_session_stats_missing_session_raises(storage):
    with pytest.raises(ValueError):
        storage.get_session_stats(999)


def test_connection_shared_across_threads(storage):
    """El worker escribe desde otro thread: check_same_thread=False + Lock.

    Una conexión sqlite ordinaria lanzaría 'created in a thread can only be
    used in that same thread'. Aquí debe funcionar y no perder escrituras.
    """
    sid = storage.start_session("generic")
    errors: list[Exception] = []

    def writer():
        try:
            for _ in range(10):
                storage.save_inspection(sid, _verdict("PASS"), _result("PASS"))
        except Exception as exc:  # noqa: BLE001 - lo reportamos al hilo principal
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"escritura cross-thread falló: {errors}"
    assert storage.get_session_stats(sid)["total_inspections"] == 40
