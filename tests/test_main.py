"""Smoke test de la orquestación de main.run_single_image (modo --image).

Ejercita el pipeline completo sin cámara ni API real: imread → preprocess →
analyze (mockeado) → decisión → storage → reporte HTML. Las rutas de datos y
reportes se redirigen a temporales para no tocar el repo.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

import main
import src.reporter as reporter_mod
from src.analyzer import Defect, InspectionResult, VisionAnalyzer

REAL_TEMPLATES = str(Path(main.__file__).parent / "templates")


def _fake_result(self, frame_b64):  # firma de VisionAnalyzer.analyze(self, frame_b64)
    return InspectionResult(
        verdict="FAIL",
        overall_confidence=0.95,
        defects=[Defect("Grieta", "critical", "centro", 0.95)],
        summary="Grieta crítica detectada",
        evaluable=True,
        raw_response="{}",
        latency_ms=100,
        input_tokens=800,
        output_tokens=90,
    )


@pytest.fixture
def isolated_main(tmp_path, monkeypatch):
    """Redirige DB, sesiones y reportes a temporales; mockea la llamada a la API."""
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "data" / "qc.db")
    monkeypatch.setattr(main, "SESSIONS_DIR", tmp_path / "data" / "sessions")

    reports_dir = tmp_path / "reports"

    def reporter_factory(template_path, output_dir):
        # Conserva el template real, redirige la salida a tmp
        return reporter_mod.Reporter(template_path, str(reports_dir))

    monkeypatch.setattr(main, "Reporter", reporter_factory)
    monkeypatch.setattr(VisionAnalyzer, "analyze", _fake_result)
    return tmp_path, reports_dir


def _write_image(path: Path) -> str:
    cv2.imwrite(str(path), np.zeros((8, 8, 3), dtype=np.uint8))
    return str(path)


def test_run_single_image_end_to_end(isolated_main, tmp_path, capsys):
    tmp, reports_dir = isolated_main
    image_path = _write_image(tmp_path / "muestra.jpg")
    settings = main.load_settings()
    profile = main.load_profile("generic")

    main.run_single_image(image_path, settings, profile, api_key="test-key")

    # Se generó exactamente un reporte HTML no vacío
    reports = list(reports_dir.glob("*.html"))
    assert len(reports) == 1
    assert reports[0].stat().st_size > 0

    # La sesión quedó persistida y cerrada, con la inspección FAIL guardada
    from src.storage import Storage

    storage = Storage(str(main.DB_PATH), str(main.SESSIONS_DIR))
    try:
        sid = storage.get_last_session_id()
        assert sid is not None
        stats = storage.get_session_stats(sid)
        assert stats["total_inspections"] == 1
        assert stats["fail_count"] == 1
        assert stats["ended_at"] is not None
        inspections = storage.get_inspections(sid)
        assert inspections[0]["verdict"] == "FAIL"
        assert inspections[0]["defects"][0]["severity"] == "critical"
    finally:
        storage.close()

    # El veredicto se imprimió por consola
    out = capsys.readouterr().out
    assert "Veredicto: FAIL" in out
    assert "Reporte:" in out


def test_run_single_image_missing_file_exits(isolated_main, tmp_path):
    settings = main.load_settings()
    profile = main.load_profile("generic")
    with pytest.raises(SystemExit):
        main.run_single_image(
            str(tmp_path / "no_existe.jpg"), settings, profile, api_key="test-key"
        )


def test_run_export_writes_csv(isolated_main, tmp_path):
    """--export vuelca las inspecciones de la última sesión a CSV con sus defectos."""
    import csv as _csv

    tmp, _ = isolated_main
    image_path = _write_image(tmp_path / "muestra.jpg")
    settings = main.load_settings()
    profile = main.load_profile("generic")
    main.run_single_image(image_path, settings, profile, api_key="test-key")

    csv_path = tmp_path / "export.csv"
    main.run_export(str(csv_path))

    assert csv_path.exists()
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["verdict"] == "FAIL"
    # El defecto crítico quedó serializado en la celda de defectos
    assert "critical" in rows[0]["defects"]
    assert "Grieta" in rows[0]["defects"]


def test_run_export_no_sessions_exits(isolated_main, tmp_path):
    """Sin sesiones registradas, --export sale con error en vez de crear un CSV vacío."""
    with pytest.raises(SystemExit):
        main.run_export(str(tmp_path / "vacio.csv"))
