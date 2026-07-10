"""Tests del VisionAnalyzer: parseo de respuestas y llamada a la API (mockeada)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.analyzer import SYSTEM_PROMPT, VisionAnalyzer, VisionVerdict

PROFILE = {
    "name": "Test",
    "inspection_criteria": "Busca defectos.",
    "context": "Producto de prueba.",
    "fail_on_severity": "major",
}

VALID_RESPONSE = json.dumps(
    {
        "verdict": "FAIL",
        "overall_confidence": 0.92,
        "evaluable": True,
        "summary": "Grieta visible en la carcasa",
        "defects": [
            {
                "description": "Grieta de 3cm",
                "severity": "major",
                "location": "esquina superior derecha",
                "confidence": 0.95,
            }
        ],
    }
)


def test_parse_valid_json_response():
    result = VisionAnalyzer._parse_response(VALID_RESPONSE, latency_ms=1200)

    assert result.verdict == "FAIL"
    assert result.overall_confidence == 0.92
    assert result.evaluable is True
    assert result.summary == "Grieta visible en la carcasa"
    assert result.latency_ms == 1200
    assert len(result.defects) == 1
    assert result.defects[0].severity == "major"
    assert result.defects[0].confidence == 0.95


def test_parse_json_wrapped_in_markdown_fences():
    # El modelo a veces envuelve el JSON en bloque de código pese al prompt
    wrapped = f"```json\n{VALID_RESPONSE}\n```"
    result = VisionAnalyzer._parse_response(wrapped)
    assert result.verdict == "FAIL"
    assert len(result.defects) == 1


def test_parse_invalid_json_falls_back_gracefully():
    result = VisionAnalyzer._parse_response("Lo siento, no puedo analizar esto.")

    assert result.verdict == "WARN"
    assert result.evaluable is False
    assert result.defects == []
    assert result.raw_response == "Lo siento, no puedo analizar esto."


def test_parse_unknown_verdict_normalizes_to_warn():
    result = VisionAnalyzer._parse_response('{"verdict": "MAYBE", "defects": []}')
    assert result.verdict == "WARN"


def _fake_parsed_response(parsed: VisionVerdict | None):
    """Imita la respuesta de messages.parse: parsed_output + content + usage."""
    return SimpleNamespace(
        parsed_output=parsed,
        content=[SimpleNamespace(type="text", text=parsed.model_dump_json() if parsed else "")],
        usage=SimpleNamespace(input_tokens=850, output_tokens=120),
    )


def test_analyze_with_real_image(monkeypatch):
    analyzer = VisionAnalyzer(api_key="test-key", profile=PROFILE)
    captured = {}
    parsed = VisionVerdict.model_validate_json(VALID_RESPONSE)

    def fake_parse(**kwargs):
        captured.update(kwargs)
        return _fake_parsed_response(parsed)

    monkeypatch.setattr(analyzer._client.messages, "parse", fake_parse)

    frame_b64 = "ZmFrZS1qcGVnLWJ5dGVz"
    result = analyzer.analyze(frame_b64)

    # Parámetros de la llamada
    assert captured["model"] == "claude-haiku-4-5"
    assert captured["system"] == SYSTEM_PROMPT
    assert captured["output_format"] is VisionVerdict
    image_block, text_block = captured["messages"][0]["content"]
    assert image_block["type"] == "image"
    assert image_block["source"]["data"] == frame_b64
    assert image_block["source"]["media_type"] == "image/jpeg"
    assert PROFILE["inspection_criteria"] in text_block["text"]
    assert PROFILE["fail_on_severity"] in text_block["text"]

    # Resultado y contadores de costo
    assert result.verdict == "FAIL"
    assert result.defects[0].severity == "major"
    assert result.input_tokens == 850
    assert analyzer.total_analyses == 1
    assert analyzer.estimated_cost_usd > 0


def test_model_is_configurable_and_updates_prices(monkeypatch):
    """El modelo pasado en el constructor se usa en la llamada y define los precios."""
    analyzer = VisionAnalyzer(api_key="test-key", profile=PROFILE, model="claude-sonnet-4-6")
    captured = {}
    parsed = VisionVerdict.model_validate_json(VALID_RESPONSE)

    def fake_parse(**kwargs):
        captured.update(kwargs)
        return _fake_parsed_response(parsed)

    monkeypatch.setattr(analyzer._client.messages, "parse", fake_parse)
    analyzer.analyze("abc")

    assert analyzer.model == "claude-sonnet-4-6"
    assert captured["model"] == "claude-sonnet-4-6"
    # Sonnet 4.6 es 3x el precio de Haiku (3/15 vs 1/5 por 1M tokens)
    haiku = VisionAnalyzer(api_key="test-key", profile=PROFILE)  # default haiku
    assert analyzer._input_price == pytest.approx(haiku._input_price * 3)
    assert analyzer._output_price == pytest.approx(haiku._output_price * 3)


def test_unknown_model_falls_back_to_default_prices():
    """Un modelo no listado en MODEL_PRICES usa el fallback (Haiku) sin romper."""
    unknown = VisionAnalyzer(api_key="test-key", profile=PROFILE, model="claude-futuro-9")
    haiku = VisionAnalyzer(api_key="test-key", profile=PROFILE)
    assert unknown._input_price == haiku._input_price
    assert unknown._output_price == haiku._output_price


PASS_RESPONSE = json.dumps(
    {
        "verdict": "PASS",
        "overall_confidence": 0.97,
        "evaluable": True,
        "summary": "Sin defectos",
        "defects": [],
    }
)


def _model_routing_parse(by_model: dict[str, str]):
    """Fake de messages.parse que devuelve un raw distinto según kwargs['model']."""
    calls = []

    def fake_parse(**kwargs):
        calls.append(kwargs["model"])
        parsed = VisionVerdict.model_validate_json(by_model[kwargs["model"]])
        return _fake_parsed_response(parsed)

    return fake_parse, calls


def test_escalation_replaces_primary_verdict(monkeypatch):
    """Con escalado, un FAIL de Haiku se re-consulta con Sonnet y su veredicto manda."""
    analyzer = VisionAnalyzer(
        api_key="test-key",
        profile=PROFILE,
        model="claude-haiku-4-5",
        escalation_model="claude-sonnet-4-6",
    )
    # Haiku dice FAIL; Sonnet (escalación) dice PASS
    fake_parse, calls = _model_routing_parse(
        {"claude-haiku-4-5": VALID_RESPONSE, "claude-sonnet-4-6": PASS_RESPONSE}
    )
    monkeypatch.setattr(analyzer._client.messages, "parse", fake_parse)

    result = analyzer.analyze("abc")

    assert calls == ["claude-haiku-4-5", "claude-sonnet-4-6"]  # ambas llamadas
    assert result.escalated is True
    assert result.model == "claude-sonnet-4-6"
    assert result.verdict == "PASS"                 # manda el veredicto de Sonnet
    assert "FAIL" in result.primary_raw_response     # el raw de Haiku queda para auditoría
    assert analyzer.total_analyses == 1              # un frame = un análisis
    # El costo suma ambas llamadas (Haiku + Sonnet, 850/120 tokens cada una)
    assert analyzer.total_input_tokens == 1700
    assert analyzer.estimated_cost_usd > 0


def test_no_escalation_on_pass(monkeypatch):
    """Un PASS del modelo primario no dispara la segunda llamada."""
    analyzer = VisionAnalyzer(
        api_key="test-key",
        profile=PROFILE,
        model="claude-haiku-4-5",
        escalation_model="claude-sonnet-4-6",
    )
    fake_parse, calls = _model_routing_parse({"claude-haiku-4-5": PASS_RESPONSE})
    monkeypatch.setattr(analyzer._client.messages, "parse", fake_parse)

    result = analyzer.analyze("abc")

    assert calls == ["claude-haiku-4-5"]  # solo el primario
    assert result.escalated is False
    assert result.model == "claude-haiku-4-5"
    assert result.verdict == "PASS"


def test_escalation_disabled_by_default(monkeypatch):
    """Sin escalation_model, nunca hay segunda llamada aunque el veredicto sea FAIL."""
    analyzer = VisionAnalyzer(api_key="test-key", profile=PROFILE, model="claude-haiku-4-5")
    fake_parse, calls = _model_routing_parse({"claude-haiku-4-5": VALID_RESPONSE})
    monkeypatch.setattr(analyzer._client.messages, "parse", fake_parse)

    result = analyzer.analyze("abc")

    assert calls == ["claude-haiku-4-5"]
    assert result.escalated is False


def test_analyze_reports_metric_when_observatory_configured(monkeypatch):
    """Con observatory_url, analyze() reporta la métrica (side-channel) sin bloquear."""
    import src.analyzer as analyzer_mod

    captured = {}

    def fake_send(url, data, token=None):
        captured["url"] = url
        captured["data"] = data
        captured["token"] = token

    # Simula el SDK instalado (los tests no dependen del paquete real)
    monkeypatch.setattr(analyzer_mod, "send_metric_background", fake_send)
    monkeypatch.setattr(analyzer_mod, "calculate_cost", lambda m, i, o: 0.01)

    analyzer = VisionAnalyzer(
        api_key="test-key",
        profile=PROFILE,
        observatory_url="https://obs.example.app",
        observatory_token="obs_sk_123",
    )
    parsed = VisionVerdict.model_validate_json(VALID_RESPONSE)
    monkeypatch.setattr(
        analyzer._client.messages, "parse", lambda **kw: _fake_parsed_response(parsed)
    )

    analyzer.analyze("abc")

    assert captured["url"] == "https://obs.example.app"
    assert captured["token"] == "obs_sk_123"
    assert captured["data"]["model"] == "claude-haiku-4-5"
    assert captured["data"]["input_tokens"] == 850
    assert captured["data"]["cost_usd"] == 0.01
    assert captured["data"]["tags"]["verdict"] == "FAIL"
    assert captured["data"]["tags"]["app"] == "visual-qc-inspector"


def test_analyze_no_metric_without_observatory_url(monkeypatch):
    """Sin observatory_url (default), no se reporta nada aunque el SDK esté."""
    import src.analyzer as analyzer_mod

    calls = []
    monkeypatch.setattr(analyzer_mod, "send_metric_background", lambda *a, **k: calls.append(a))

    analyzer = VisionAnalyzer(api_key="test-key", profile=PROFILE)  # sin observatory_url
    parsed = VisionVerdict.model_validate_json(VALID_RESPONSE)
    monkeypatch.setattr(
        analyzer._client.messages, "parse", lambda **kw: _fake_parsed_response(parsed)
    )

    analyzer.analyze("abc")
    assert calls == []


def test_analyze_falls_back_when_unparseable(monkeypatch):
    """Si la API no devuelve salida parseable (refusal/max_tokens), degrada a WARN."""
    analyzer = VisionAnalyzer(api_key="test-key", profile=PROFILE)

    def fake_parse(**kwargs):
        return _fake_parsed_response(None)

    monkeypatch.setattr(analyzer._client.messages, "parse", fake_parse)
    result = analyzer.analyze("abc")

    assert result.verdict == "WARN"
    assert result.evaluable is False


def test_analyze_propagates_api_errors(monkeypatch):
    """Los errores de API deben propagarse (el worker los maneja, no el analyzer)."""
    analyzer = VisionAnalyzer(api_key="test-key", profile=PROFILE)

    def fake_parse(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(analyzer._client.messages, "parse", fake_parse)
    with pytest.raises(RuntimeError):
        analyzer.analyze("abc")
