# Roadmap v2

Objetivo de la v2: sobre la base funcional de v1 (pipeline webcam → Haiku 4.5 →
veredicto con enforcement de severidad → evidencia auditable), mejorar la
**precisión medible**, la **robustez operativa** y la **integración** con el
exterior, y completar los pendientes de pulido.

Las fases están ordenadas por relación valor/esfuerzo. Cada ítem es
independiente y entregable por separado.

---

## Fase 1 — Precisión medible (core de v2)

### 1.1 Golden set de evaluación
Hoy no hay forma objetiva de saber si un cambio de modelo/prompt mejora o
empeora. Crear `eval/` con:
- Imágenes etiquetadas a mano (`eval/images/` + `eval/labels.yaml`: veredicto
  esperado y severidad esperada por imagen).
- Script `eval/run_eval.py` que corre el pipeline completo sobre el set y
  reporta accuracy de veredicto, precision/recall de FAIL y matriz de
  confusión de severidades.
- Es el prerequisito para decidir con datos cualquier cambio futuro
  (¿alcanza Haiku con las rúbricas? ¿cuánto mejora el escalado híbrido?).

### 1.2 Escalado híbrido Haiku → Sonnet
Haiku analiza el 100% de los frames (barato). Cuando devuelve WARN o FAIL,
disparar una segunda llamada de confirmación con Sonnet solo sobre ese frame.
Los WARN/FAIL son minoría → costo extra marginal, precisión de Sonnet donde
importa.
- Config en `settings.yaml`: `api.escalation_model` (vacío = desactivado) y
  `api.escalate_on: [WARN, FAIL]`.
- El veredicto de Sonnet reemplaza al de Haiku; guardar ambos `raw_response`
  en SQLite para auditoría (columna nueva `escalated_response`).
- Medir el impacto con el golden set de 1.1 antes y después.

### 1.3 Modelo configurable en settings.yaml
Sacar la constante `MODEL` hardcodeada de `src/analyzer.py`:
- `api.model` en `settings.yaml` (default `claude-haiku-4-5`).
- Tabla de precios por modelo en un dict (`MODEL_PRICES`) para que el costo
  del dashboard siga siendo correcto al cambiar de modelo sin tocar código.

## Fase 2 — Robustez operativa

### 2.1 Modo batch
`python main.py --dir carpeta/` para analizar un lote de imágenes de una vez
(reutiliza `run_single_image`, genera una sola sesión y un solo reporte).
Útil para re-procesar evidencia o validar un lote de producción offline.

### 2.2 Export CSV
`python main.py --export sesión.csv [--session N]`: volcar inspecciones desde
SQLite a CSV (timestamp, veredicto, confianza, defectos, ruta del frame) para
análisis en Excel/pandas sin tocar la DB.

### 2.3 Retención de evidencia
`storage.retention_days` en settings: al iniciar, borrar frames y sesiones
más viejas que N días (0 = nunca). Evita que `data/sessions/` crezca sin
límite en uso continuo.

### 2.4 Reconexión de cámara
Si `cam.read()` falla en vivo (webcam desconectada), reintentar apertura con
backoff en lugar de terminar. Mostrar estado "CAMARA DESCONECTADA" en el
dashboard mientras tanto.

## Fase 3 — Integración

### 3.1 Verificar observabilidad llm-observatory
Ya está integrada como side-channel; falta verificar en vivo que las métricas
llegan al dashboard remoto con el modelo nuevo y documentar qué se ve ahí.

### 3.2 Webhook enriquecido
El payload actual es solo texto. Agregar: nombre del perfil, lista de
defectos con severidad/confianza, y opcionalmente el frame como adjunto
(Discord soporta multipart) o link al archivo local.

### 3.3 CI con GitHub Actions
Workflow que corre `pytest` en cada push/PR (los tests ya no necesitan
webcam ni API key — mockean todo). Badge en el README.

### 3.4 API REST mínima (opcional, evaluar si hace falta)
FastAPI de solo lectura: `GET /sessions`, `GET /sessions/{id}/inspections`,
servir los reportes HTML. Solo si aparece la necesidad de consultar resultados
desde otra máquina; si no, el reporte HTML standalone ya cubre el caso.

## Fase 4 — Completar y pulir

### 4.1 Empaquetado
`pyproject.toml` con dependencias y entry point (`visual-qc` como comando),
reemplazando el `requirements.txt` plano.

### 4.2 Dashboard: estado del debounce
Mostrar en el overlay la racha actual (`consecutive_count`/`debounce_frames`)
y el `fail_on_severity` del perfil activo, para que el operador entienda por
qué un WARN todavía no es FAIL.

### 4.3 Guía de calibración de perfiles
Documento corto: cómo escribir una rúbrica de severidad efectiva, cómo usar
el golden set (1.1) para validar un perfil nuevo, y qué tocar cuando hay
falsos positivos vs falsos negativos.

## Fase 5 — Multi-proveedor: Gemini como backend de visión (futura)

**Estado: planificada, no implementada.** Objetivo: que `api.provider` en
`settings.yaml` elija entre `claude` (default, actual) y `gemini`, sin perder
Claude ni el escalado híbrido. Se documenta acá para una fase siguiente; hay una
API key de Gemini disponible pero el modelo exacto queda por decidir (evaluar
`gemini-2.5-flash` como económico vs `gemini-2.5-pro` por precisión, con el
golden set de 1.1).

Motivación: hoy el proveedor está hardcodeado en el ecosistema Claude (decisión
de v1, ver CHANGELOG). Una abstracción de proveedor permite A/B real de precisión
y costo Claude vs Gemini sobre el mismo golden set, y da opción de segunda fuente.

### 5.1 Abstracción de proveedor
Introducir una interfaz mínima (ej. `VisionProvider`) con un único método
`analyze_frame(frame_b64, profile) -> InspectionResult`. `VisionAnalyzer` deja de
hablar con el SDK `anthropic` directamente y delega en el provider activo. Lo que
**no** cambia y debe seguir igual: `InspectionResult`, `DecisionEngine`, worker,
storage, dashboard — el contrato de salida es el mismo.

### 5.2 Provider Gemini (`google-genai`)
Puntos concretos a resolver (todos hoy asumen Anthropic en `src/analyzer.py`):
- **SDK/cliente:** `google-genai` en vez de `anthropic.Anthropic`. Dep opcional
  como `llm-observatory` (import con fallback): si falta el paquete o la key, el
  provider Gemini no está disponible pero no rompe la app.
- **Structured outputs:** el equivalente a `messages.parse(..., output_format=VisionVerdict)`
  es `generate_content` con `response_mime_type="application/json"` +
  `response_schema` (el mismo modelo Pydantic `VisionVerdict` debería servir como
  schema). Mantener el fallback a `_parse_response()` → WARN no evaluable ante
  refusal/truncado, igual que hoy.
- **Imagen:** Anthropic usa un bloque `{"type":"image","source":{"type":"base64",
  "media_type":"image/jpeg","data":...}}`; Gemini usa `Part.from_bytes(data, mime_type)`
  o inline_data. Reutilizar el JPEG que ya produce `Preprocessor` (no re-encodear).
- **Precios/costo:** agregar filas Gemini a `MODEL_PRICES` (o un dict por proveedor)
  para que el costo estimado del dashboard siga siendo correcto. Verificar unidades
  (USD/1M tokens) contra el pricing vigente de Gemini.
- **Observabilidad:** `_report_metric()` replica hoy el esquema del SDK de
  llm-observatory con `model`/tokens/`cost_usd`/`latency_ms`/`tags`; mantenerlo,
  agregando `provider` como tag.

### 5.3 API key y config
- Key vía env `GEMINI_API_KEY` (agregar a `.env.example`), leída en `main.py` junto
  a `ANTHROPIC_API_KEY`. Nunca hardcodear.
- `api.provider: claude|gemini` en `settings.yaml`; `build_components()` instancia el
  provider correcto. `api.model` pasa a interpretarse según el provider activo.

### 5.4 Escalado híbrido cross-proveedor (opcional)
El escalado actual (`api.escalation_model`) asume el mismo proveedor. Evaluar si el
modelo de escalado puede ser de otro proveedor (ej. primario Gemini flash → escala a
Claude Sonnet). Requiere que `escalation_model` resuelva también su provider. Dejar
para el final; el escalado mismo-proveedor cubre el caso común.

### 5.5 Tests y evaluación
- Los tests mockean `analyzer._client.messages.parse`; con la abstracción, mockear a
  nivel del provider (o de su cliente Gemini) replicando el objeto de respuesta.
- Correr el golden set (1.1) con `api.provider: gemini` y comparar accuracy/F1/costo
  contra Claude **antes** de considerar cambiar el default. Es la decisión con datos.

---

## Orden sugerido de implementación

1. **1.3** (modelo configurable) — chico, desbloquea el resto.
2. **1.1** (golden set) — sin esto no se puede medir nada de lo que sigue.
3. **1.2** (escalado híbrido) — la mejora de precisión de mayor impacto.
4. **2.x** en orden — robustez para uso continuo.
5. **3.3** (CI) en cualquier momento — es independiente.
6. **3.1, 3.2, 4.x** — según necesidad.
7. **3.4** solo si aparece el caso de uso real.
8. **5.x** (multi-proveedor Gemini) — fase futura; empezar por 5.1 (abstracción) +
   5.2 (provider) + 5.3 (config), medir con el golden set (5.5) antes de tocar el
   default, y dejar 5.4 (escalado cross-proveedor) para el final.
