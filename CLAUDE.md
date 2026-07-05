# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es

Sistema de inspección visual de calidad en tiempo real: la webcam captura frames, Claude Vision analiza la imagen contra criterios de inspección, y el sistema emite veredictos **PASS / WARN / FAIL** con evidencia auditable (frame guardado + JSON + reporte de sesión).

Principio de diseño central: **no hay reglas de visión manuales en código**. Los criterios de inspección son texto en lenguaje natural dentro de perfiles YAML — cualquier persona puede agregar un perfil de producto nuevo sin tocar Python. Claude Vision interpreta la imagen contra esos criterios.

## Arquitectura

Pipeline por frame:

1. **Captura** — `CameraCapture` (OpenCV) lee frames de la webcam; `Dashboard` renderiza overlay en vivo (último veredicto, FPS, stats, costo estimado). ROI opcional: si `roi.enabled`, se selecciona una región interactiva al inicio (`cam.select_roi`).
2. **Preprocesado** — `Preprocessor` reduce el frame a `target_width`, codifica JPEG (`jpeg_quality`) y opcionalmente aplica CLAHE (`enhance_contrast`). Menos píxeles = menos tokens.
3. **Selección de frame** — `FrameSelector` decide qué frames se analizan (throttle): modo `timer` (cada N s), `diff` (cambio de escena > umbral) o `manual` (solo tecla SPACE). Evita gastar llamadas a la API en cada frame.
4. **Perfil** — PyYAML carga el perfil del producto activo: criterios en texto natural, `context` y `fail_on_severity`.
5. **Análisis** — el frame (base64) + criterios del perfil se envían a Claude Vision con structured outputs; la respuesta valida contra el esquema Pydantic `VisionVerdict` (veredicto + confianza + defectos).
6. **Veredicto y alerta** — `DecisionEngine` ajusta por confianza y debounce; `Alerter` dispara en FAIL confirmado: sonido (`afplay` mac / `winsound` win / campana `\a` como fallback), notificación del OS (`osascript` en mac) y webhook Slack/Discord (`WEBHOOK_URL`). PASS puede disparar sonido si `sound_on_pass`.
7. **Evidencia** — cada inspección guarda en SQLite (`data/qc.db`) y, según `storage.save_*_frames`, el frame en `data/sessions/<id>/frames/`.
8. **Reporte** — al cerrar la sesión, jinja2 genera un reporte HTML standalone en `reports/`.

Separación clave: la lógica de captura/dashboard (OpenCV, loop en tiempo real) debe estar desacoplada del cliente de análisis (llamadas a la API, que tienen latencia de segundos). No bloquear el loop de video esperando la API — analizar frames muestreados, no cada frame.

## Stack

- Python 3.9+ (el venv del repo corre 3.9.6; el código usa `from __future__ import annotations` para sintaxis de tipos moderna en 3.9). El CLAUDE.md previo decía 3.11+ — verificar antes de asumir features de 3.10/3.11.
- OpenCV (`cv2`) — captura y dashboard
- SDK oficial `anthropic` — Claude Vision (`pydantic` para el esquema de structured outputs)
- PyYAML — perfiles de inspección; `python-dotenv` carga `.env`
- SQLite (`sqlite3` estándar) — resultados
- jinja2 — reportes HTML; `requests` — webhooks de alerta
- pytest — tests

## Uso de la API de Claude

- **Modelo: `claude-sonnet-4-6`** (constante `MODEL` en `src/analyzer.py`; decisión del proyecto: balance velocidad/costo). El ID es exacto — no agregar sufijos de fecha. Los precios por token (`INPUT_TOKEN_PRICE`/`OUTPUT_TOKEN_PRICE`) alimentan el costo estimado del dashboard: actualízalos si cambia el modelo.
- API key vía `ANTHROPIC_API_KEY`; `main.py` llama `load_dotenv()` y lee un `.env` (ver `.env.example`, que incluye también `WEBHOOK_URL` opcional). El cliente `anthropic.Anthropic(api_key=...)` la recibe explícita. Nunca hardcodearla.
- Imágenes: bloque de contenido `{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": ...}}` seguido del bloque de texto con los criterios.
- Structured outputs vía `client.messages.parse(..., output_format=VisionVerdict)` (`VisionVerdict` es un modelo Pydantic). Se usa `response.parsed_output`; si viene `None` (refusal / max_tokens), se cae a `_parse_response()` que degrada a WARN no evaluable. Nunca se pide JSON por prompt.
- Los criterios del perfil van en el prompt; mantener la parte estable (system prompt / instrucciones del perfil) idéntica entre llamadas para aprovechar prompt caching.
- El SDK ya reintenta 429/408/409/5xx con backoff exponencial y respeta `retry-after`; `max_retries` es configurable vía `api.max_retries` en settings (default 3, sube el default 2 del SDK). No implementar retry manual encima. Lo que el SDK no resuelva lo captura el worker (`except anthropic.APIError`) — un error de API no tumba el loop; se registra `last_error` y se continúa.
- **Prompt caching no aplica acá** (aunque el system prompt sea estable): el prefijo cacheable mínimo de Sonnet 4.6 es 2048 tokens y el `SYSTEM_PROMPT` es ~150; además la imagen (el grueso de tokens) cambia cada frame e invalida el prefijo. `cache_control` no daría error pero `cache_creation_input_tokens` sería 0 — sería código muerto.

## Perfiles de inspección (YAML)

Un perfil define un producto inspeccionable. Campos usados por el código (`config/profiles/*.yaml`, ver `generic`/`pcb`/`packaging`): `name`, `description`, `version`, `inspection_criteria` (texto natural, va al prompt), `context` (contexto adicional) y `fail_on_severity` (`critical`/`major`/`minor`/`cosmetic`; el umbral de severidad que dispara FAIL). Un archivo por producto; el activo por defecto es `active_profile` en `config/settings.yaml`. Agregar un producto = agregar un YAML, sin tocar Python. En vivo, la tecla `P` rota entre perfiles disponibles.

## Comandos

```bash
python main.py                       # inspección en vivo (active_profile de config/settings.yaml)
python main.py --profile pcb         # perfil específico
python main.py --image foto.jpg      # analizar una imagen estática (sin cámara)
python main.py --report              # regenerar reporte de la última sesión

pytest                               # todos los tests
pytest tests/test_decision.py -v     # un archivo
pytest tests/test_analyzer.py::test_parse_valid_json_response  # un test
```

Teclas en vivo (ventana OpenCV): `SPACE` análisis manual · `Q` salir + reporte · `R` reporte de la sesión actual · `P` siguiente perfil · `D` toggle detalle de defectos · `S` screenshot.

Los tests no requieren webcam ni API key real — el cliente `anthropic` se mockea con `monkeypatch` sobre `analyzer._client.messages.parse` (structured outputs; **no** `.create`). El fake devuelve un `SimpleNamespace` con `parsed_output`, `content` y `usage`.

## Estructura y flujo de datos

- `main.py` orquesta: `run_live` (cámara), `run_single_image` (--image), `run_report_only` (--report). `build_components()` arma todo desde `settings` (cada sección de `settings.yaml` mapea a un `__init__` por `**kwargs`).
- El loop de video (30fps) nunca espera a la API: `src/worker.py` (`AnalysisWorker`) corre en un thread con `queue.Queue(maxsize=1)` — si llega un frame mientras se analiza otro, se descarta. El estado compartido (`get_latest()`, `is_analyzing`) está protegido por Lock. El loop solo hace `worker.submit()` cuando `not is_analyzing` **y** `frame_selector.should_analyze()`.
- Errores de API los captura el worker (`except anthropic.APIError`), registra `last_error` y sigue; cualquier otra excepción se imprime y también se traga — el loop nunca cae.
- `VisionAnalyzer.analyze()` propaga errores de API; es el worker quien los captura para no tumbar el loop.
- `DecisionEngine` aplica dos capas sobre el veredicto crudo de Claude: degradación por confianza (FAIL < `fail_threshold` → WARN; WARN < `warn_threshold` → PASS) y debounce (FAIL firme solo tras N consecutivos; defecto `critical` con confianza alta = FAIL inmediato sin debounce).
- `Alerter` (`src/alerter.py`) dispara **solo** cuando `verdict.status == "FAIL" and verdict.is_confirmed` (o PASS si `sound_on_pass`). Todas las alertas corren en threads daemon y tragan sus excepciones — una alerta fallida nunca afecta la inspección. El webhook (`_send_webhook`) postea payload compatible Slack/Discord (`{"text", "content"}`) a `WEBHOOK_URL`.
- `Storage` comparte una conexión SQLite entre threads (`check_same_thread=False` + Lock). Dos tablas: `sessions` y `inspections` (incluye `raw_response`: el JSON crudo de Claude, para auditoría). `_migrate()` agrega columnas nuevas a DBs de versiones previas con `ALTER TABLE` (idempotente vía `PRAGMA table_info`). Guardar el frame de evidencia depende de `storage.save_{pass,warn,fail}_frames`; va a `data/sessions/<id>/frames/`, los reportes HTML standalone a `reports/`.
- El parseo de la respuesta de Claude está aislado en `VisionAnalyzer._parse_response()` (estático, fácil de testear): limpia fences de markdown, aísla el objeto JSON, y ante cualquier fallo retorna WARN con `evaluable=False` — nunca lanza.
- El dashboard usa solo ASCII en overlays: las fuentes Hershey de cv2 no renderizan unicode (✓✗⚠ saldrían como "?").
