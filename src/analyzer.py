"""Cliente de Claude Vision: envía el frame y parsea el veredicto JSON."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

# Observabilidad opcional (llm-observatory). El SDK solo instrumenta
# messages.create(); como acá usamos messages.parse() (structured outputs),
# integramos como side-channel: reportamos la métrica nosotros con el mismo
# esquema del SDK. Si el paquete no está instalado, la observabilidad es no-op.
try:
    from llm_observatory import calculate_cost
    from llm_observatory._utils import send_metric_background
except ImportError:  # pragma: no cover - depende de un paquete opcional
    calculate_cost = None
    send_metric_background = None

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024

# Precios de Claude Sonnet 4.6 (USD por token) para el costo estimado en dashboard
INPUT_TOKEN_PRICE = 3.00 / 1_000_000
OUTPUT_TOKEN_PRICE = 15.00 / 1_000_000

# El formato de la respuesta lo garantizan los structured outputs (ver VisionVerdict),
# no el prompt: aquí solo describimos la tarea y la semántica de los campos.
SYSTEM_PROMPT = """
Eres un sistema experto de inspección visual de control de calidad industrial.
Analizas imágenes y determinas si el producto pasa el control de calidad,
devolviendo un veredicto estructurado (PASS / WARN / FAIL) con los defectos detectados.

Si la imagen no es evaluable (muy oscura, borrosa, ángulo incorrecto), marca
evaluable=false, usa verdict="WARN" y explica en summary por qué no es evaluable.
El campo summary debe ser una frase corta de máximo 10 palabras.
"""


class DefectSchema(BaseModel):
    """Esquema de un defecto para el structured output de Claude Vision."""

    description: str = Field(description="Descripción del defecto en lenguaje natural")
    severity: Literal["critical", "major", "minor", "cosmetic"]
    location: str = Field(description="Dónde en la imagen, ej. 'esquina superior derecha'")
    confidence: float = Field(description="Confianza de 0.0 a 1.0")


class VisionVerdict(BaseModel):
    """Esquema completo de la respuesta de inspección (structured output)."""

    verdict: Literal["PASS", "WARN", "FAIL"]
    overall_confidence: float = Field(description="Confianza global de 0.0 a 1.0")
    evaluable: bool = Field(description="False si la imagen no es evaluable")
    summary: str = Field(description="Frase corta de resumen, máximo 10 palabras")
    defects: list[DefectSchema] = Field(default_factory=list)


@dataclass
class Defect:
    description: str       # descripción en lenguaje natural
    severity: str          # "critical" | "major" | "minor" | "cosmetic"
    location: str          # ej. "esquina superior derecha"
    confidence: float      # 0.0 a 1.0


@dataclass
class InspectionResult:
    verdict: str           # "PASS" | "WARN" | "FAIL"
    overall_confidence: float
    defects: list[Defect]
    summary: str           # frase corta de resumen
    evaluable: bool        # False si la imagen no es evaluable
    raw_response: str      # texto completo de Claude (para debug)
    timestamp: datetime = field(default_factory=datetime.now)
    latency_ms: int = 0
    # Tokens reales reportados por la API — alimentan el costo estimado
    input_tokens: int = 0
    output_tokens: int = 0


class VisionAnalyzer:
    def __init__(
        self,
        api_key: str,
        profile: dict,
        max_retries: int = 3,
        observatory_url: str = "",
        observatory_token: str = "",
    ):
        # El SDK reintenta 429/408/409/5xx con backoff exponencial y respeta
        # `retry-after`. Subimos el default (2) para más resiliencia en sesiones
        # largas; el worker captura lo que quede sin tumbar el loop.
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries)
        self.profile = profile
        # Observabilidad: activa solo si hay URL configurada y el SDK instalado.
        self._observatory_url = observatory_url
        self._observatory_token = observatory_token
        # Acumuladores de sesión para el contador de costo del dashboard
        self.total_analyses = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def set_profile(self, profile: dict) -> None:
        self.profile = profile

    @property
    def estimated_cost_usd(self) -> float:
        return (
            self.total_input_tokens * INPUT_TOKEN_PRICE
            + self.total_output_tokens * OUTPUT_TOKEN_PRICE
        )

    def _build_user_message(self) -> str:
        return f"""
{self.profile['inspection_criteria']}

Contexto adicional: {self.profile.get('context', '')}

Criterios de FAIL: cualquier defecto de severidad "{self.profile.get('fail_on_severity', 'major')}" o mayor.
Criterios de WARN: defectos menores o incertidumbre.
Criterios de PASS: sin defectos o solo defectos cosméticos tolerables.

Analiza la imagen adjunta y responde con el JSON de inspección.
"""

    def analyze(self, frame_b64: str) -> InspectionResult:
        """Envía el frame a Claude Vision y retorna el resultado parseado.

        Usa structured outputs (``messages.parse`` con el esquema ``VisionVerdict``):
        la API garantiza un JSON que valida contra el esquema, sin parseo manual.

        Los errores de API se propagan: el llamador (AnalysisWorker) decide
        cómo seguir sin tumbar el loop de captura.
        """
        start = time.monotonic()
        response = self._client.messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": frame_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": self._build_user_message(),
                        },
                    ],
                }
            ],
            output_format=VisionVerdict,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        raw_text = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        parsed = response.parsed_output
        if parsed is not None:
            result = self._result_from_parsed(parsed, raw_text, latency_ms)
        else:
            # Sin salida parseable (p. ej. refusal o max_tokens): salvar o degradar a WARN
            result = self._parse_response(raw_text, latency_ms)

        result.input_tokens = getattr(response.usage, "input_tokens", 0)
        result.output_tokens = getattr(response.usage, "output_tokens", 0)
        self.total_analyses += 1
        self.total_input_tokens += result.input_tokens
        self.total_output_tokens += result.output_tokens
        self._report_metric(result)
        return result

    def _report_metric(self, result: InspectionResult) -> None:
        """Reporta la métrica a llm-observatory (fire-and-forget, no bloquea).

        Side-channel: replica el esquema que MonitoredAnthropic postea a
        /api/metrics. No-op si no hay URL configurada o el SDK no está instalado;
        cualquier fallo se traga — la observabilidad nunca afecta la inspección.
        """
        if not (self._observatory_url and send_metric_background is not None):
            return
        try:
            in_t, out_t = result.input_tokens, result.output_tokens
            cost = (
                calculate_cost(MODEL, in_t, out_t)
                if calculate_cost is not None
                else in_t * INPUT_TOKEN_PRICE + out_t * OUTPUT_TOKEN_PRICE
            )
            metric = {
                "model": MODEL,
                "input_tokens": in_t,
                "output_tokens": out_t,
                "total_tokens": in_t + out_t,
                "cost_usd": cost,
                "latency_ms": result.latency_ms,
                "status_code": 200,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "tools_used": [],
                "prompt_preview": f"[QC] {self.profile.get('name', '')}",
                "tags": {
                    "app": "visual-qc-inspector",
                    "profile": self.profile.get("name", ""),
                    "verdict": result.verdict,
                },
            }
            send_metric_background(
                self._observatory_url, metric, token=self._observatory_token or None
            )
        except Exception:  # noqa: BLE001 - la observabilidad nunca tumba el análisis
            pass

    @staticmethod
    def _result_from_parsed(
        parsed: VisionVerdict, raw_text: str, latency_ms: int
    ) -> InspectionResult:
        """Convierte el structured output validado al dataclass interno."""
        defects = [
            Defect(
                description=d.description,
                severity=d.severity,
                location=d.location,
                confidence=d.confidence,
            )
            for d in parsed.defects
        ]
        return InspectionResult(
            verdict=parsed.verdict,
            overall_confidence=parsed.overall_confidence,
            defects=defects,
            summary=parsed.summary,
            evaluable=parsed.evaluable,
            raw_response=raw_text or parsed.model_dump_json(),
            latency_ms=latency_ms,
        )

    @staticmethod
    def _parse_response(raw_text: str, latency_ms: int = 0) -> InspectionResult:
        """Parsea el JSON de Claude. Si falla, retorna WARN no evaluable."""
        text = raw_text.strip()
        # Limpiar bloques de código markdown si el modelo los agregó igual
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        # Aislar el objeto JSON por si hay texto alrededor
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return InspectionResult(
                verdict="WARN",
                overall_confidence=0.0,
                defects=[],
                summary="Respuesta del modelo no parseable",
                evaluable=False,
                raw_response=raw_text,
                latency_ms=latency_ms,
            )

        defects = [
            Defect(
                description=str(d.get("description", "")),
                severity=str(d.get("severity", "minor")),
                location=str(d.get("location", "")),
                confidence=float(d.get("confidence", 0.0)),
            )
            for d in data.get("defects", [])
            if isinstance(d, dict)
        ]

        verdict = str(data.get("verdict", "WARN")).upper()
        if verdict not in ("PASS", "WARN", "FAIL"):
            verdict = "WARN"

        return InspectionResult(
            verdict=verdict,
            overall_confidence=float(data.get("overall_confidence", 0.0)),
            defects=defects,
            summary=str(data.get("summary", "")),
            evaluable=bool(data.get("evaluable", True)),
            raw_response=raw_text,
            latency_ms=latency_ms,
        )
