"""Tests del webhook enriquecido de Alerter (sin red real).

Se captura el payload interceptando requests.post; no se ejercitan sonido ni
notificación del OS (side effects de subprocess), solo la construcción del texto.
"""

from __future__ import annotations

import json

import src.alerter as alerter_mod
from src.alerter import Alerter
from src.analyzer import Defect, InspectionResult


def _result(defects=None, summary="Grieta en carcasa", confidence=0.93):
    return InspectionResult(
        verdict="FAIL",
        overall_confidence=confidence,
        defects=defects if defects is not None else [
            Defect("Grieta longitudinal", "critical", "borde superior", 0.95),
            Defect("Rayón leve", "minor", "centro", 0.7),
        ],
        summary=summary,
        evaluable=True,
        raw_response="{}",
        latency_ms=120,
    )


def _capture_payload(monkeypatch):
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json.loads(data)
        return None

    monkeypatch.setattr(alerter_mod.requests, "post", fake_post)
    return captured


def test_webhook_includes_profile_and_defects(monkeypatch):
    captured = _capture_payload(monkeypatch)
    alerter = Alerter(
        {"alerts": {"webhook_on_fail": True}}, webhook_url="http://hook.test"
    )
    alerter._send_webhook(_result(), profile_name="pcb")

    text = captured["payload"]["text"]
    assert captured["payload"]["content"] == text  # compat Slack + Discord
    assert "[pcb]" in text                          # perfil
    assert "Grieta en carcasa" in text              # resumen
    assert "93%" in text                            # confianza global
    assert "[critical] Grieta longitudinal" in text
    assert "borde superior" in text                 # ubicación
    assert "95%" in text                            # confianza del defecto
    assert "[minor] Rayón leve" in text
    assert "Defectos (2)" in text


def test_webhook_without_defects(monkeypatch):
    captured = _capture_payload(monkeypatch)
    alerter = Alerter(
        {"alerts": {"webhook_on_fail": True}}, webhook_url="http://hook.test"
    )
    alerter._send_webhook(_result(defects=[]), profile_name="")

    text = captured["payload"]["text"]
    assert "sin detalle" in text
    assert "Defectos (0)" in text
    assert "[]" not in text  # no debe filtrarse una lista vacía cruda
