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

- **Python 3.10+** (el venv del repo corre 3.12.13). El código propio usa `from __future__ import annotations`, pero la dep opcional `llm-observatory` usa `X | None` como anotación evaluada en runtime → **requiere 3.10+** (en 3.9 falla al importar con `TypeError`, no `ImportError`). El venv se estandarizó en 3.12; sin el SDK o en 3.9 la observabilidad degrada a no-op sin tumbar la app.
- OpenCV (`cv2`) — captura y dashboard
- SDK oficial `anthropic` — Claude Vision (`pydantic` para el esquema de structured outputs)
- PyYAML — perfiles de inspección; `python-dotenv` carga `.env`
- SQLite (`sqlite3` estándar) — resultados
- jinja2 — reportes HTML; `requests` — webhooks de alerta
- pytest — tests

## Uso de la API de Claude

- **Modelo: configurable vía `api.model` en `settings.yaml`** (default `claude-haiku-4-5`, la constante `MODEL` en `src/analyzer.py`; decisión del proyecto: minimizar costo — Haiku es ~3x más barato que Sonnet 4.6 en ambos sentidos de tokens). El ID es exacto — no agregar sufijos de fecha. `build_components()` lo pasa a `VisionAnalyzer(model=...)`, que deriva los precios por token de `MODEL_PRICES` (dict modelo→(input,output) USD/1M tokens) para el costo estimado del dashboard. Al usar un modelo nuevo, agregar su fila a `MODEL_PRICES`; si falta, se usa el fallback de Haiku (el costo mostrado será aproximado pero no rompe).
- **Escalado híbrido (opcional):** `api.escalation_model` en `settings.yaml` (vacío = desactivado) + `api.escalate_on` (default `[WARN, FAIL]`). Si el modelo primario (barato) devuelve un veredicto en `escalate_on`, `VisionAnalyzer.analyze()` hace una segunda llamada con el modelo caro sobre el mismo frame y **su veredicto reemplaza al primario**. Como PASS es la mayoría, el costo extra es marginal y se recupera precisión donde importa. El raw del primario queda en `InspectionResult.primary_raw_response` y se persiste en la columna `primary_response` de SQLite (auditoría); la columna `model` guarda qué modelo produjo el veredicto final. El costo se acumula por llamada con el precio de cada modelo (`total_cost_usd`), no `tokens × un solo precio` — necesario porque el escalado mezcla modelos de distinto precio.
- API key vía `ANTHROPIC_API_KEY`; `main.py` llama `load_dotenv()` y lee un `.env` (ver `.env.example`, que incluye también `WEBHOOK_URL` opcional). El cliente `anthropic.Anthropic(api_key=...)` la recibe explícita. Nunca hardcodearla.
- Imágenes: bloque de contenido `{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": ...}}` seguido del bloque de texto con los criterios.
- Structured outputs vía `client.messages.parse(..., output_format=VisionVerdict)` (`VisionVerdict` es un modelo Pydantic). Se usa `response.parsed_output`; si viene `None` (refusal / max_tokens), se cae a `_parse_response()` que degrada a WARN no evaluable. Nunca se pide JSON por prompt.
- Los criterios del perfil van en el prompt; mantener la parte estable (system prompt / instrucciones del perfil) idéntica entre llamadas para aprovechar prompt caching.
- El SDK ya reintenta 429/408/409/5xx con backoff exponencial y respeta `retry-after`; `max_retries` es configurable vía `api.max_retries` en settings (default 3, sube el default 2 del SDK). No implementar retry manual encima. Lo que el SDK no resuelva lo captura el worker (`except anthropic.APIError`) — un error de API no tumba el loop; se registra `last_error` y se continúa.
- **Prompt caching no aplica acá** (aunque el system prompt sea estable): el prefijo cacheable mínimo de Haiku 4.5 es 4096 tokens y el `SYSTEM_PROMPT` es ~150; además la imagen (el grueso de tokens) cambia cada frame e invalida el prefijo. `cache_control` no daría error pero `cache_creation_input_tokens` sería 0 — sería código muerto.
- **Observabilidad (llm-observatory, opcional):** integrada como *side-channel* en `VisionAnalyzer._report_metric()`. El SDK `MonitoredAnthropic` solo instrumenta `messages.create()` y nosotros usamos `messages.parse()`, así que en vez de reemplazar el cliente reportamos la métrica nosotros con `send_metric_background()` replicando el esquema del SDK (model, tokens, `cost_usd`, `latency_ms`, `tags`). Es fire-and-forget y todo va en `try/except` — nunca afecta la inspección. Se activa solo si `OBSERVATORY_URL` está seteada **y** el paquete `llm-observatory` está instalado; si falta cualquiera de los dos, es no-op (import opcional con fallback a `None`). Config vía env: `OBSERVATORY_URL` / `OBSERVATORY_TOKEN`. Instalar: `pip install "git+https://github.com/DavidAucancela/llm-observatory.git#subdirectory=packages/sdk-python"`.

## Perfiles de inspección (YAML)

Un perfil define un producto inspeccionable. Campos usados por el código (`config/profiles/*.yaml`, ver `generic`/`pcb`/`packaging`): `name`, `description`, `version`, `inspection_criteria` (texto natural, va al prompt), `context` (contexto adicional) y `fail_on_severity` (`critical`/`major`/`minor`/`cosmetic`; el umbral de severidad que dispara FAIL — ver enforcement en `DecisionEngine` arriba). Un archivo por producto; el activo por defecto es `active_profile` en `config/settings.yaml`. Agregar un producto = agregar un YAML, sin tocar Python. En vivo, la tecla `P` rota entre perfiles disponibles.

Los 3 perfiles actuales tienen `fail_on_severity: minor` (más estricto que el default histórico `major`) y una **rúbrica de severidad explícita con ejemplos concretos por dominio** en el campo `context`, con la regla "ante la duda, clasifica en el nivel más severo". Esto es una mitigación de prompt (ver Uso de la API de Claude arriba: `MODEL = claude-haiku-4-5`) — Haiku tiende a subestimar severidad más que Sonnet, y la rúbrica explícita + `fail_on_severity` bajo + el enforcement de `DecisionEngine` compensan esa pérdida de precisión sin subir de modelo. Al agregar un perfil nuevo, seguir el mismo patrón de rúbrica si el producto lo amerita.

## Comandos

```bash
python main.py                       # inspección en vivo (active_profile de config/settings.yaml)
python main.py --profile pcb         # perfil específico
python main.py --image foto.jpg      # analizar una imagen estática (sin cámara)
python main.py --report              # regenerar reporte de la última sesión

pytest                               # todos los tests
pytest tests/test_decision.py -v     # un archivo
pytest tests/test_analyzer.py::test_parse_valid_json_response  # un test

python eval/run_eval.py              # evaluar precisión contra el golden set
python eval/run_eval.py --model claude-sonnet-4-6   # A/B de modelos
```

**Evaluación de precisión (`eval/`):** golden set de imágenes etiquetadas a mano
(`eval/labels.yaml`) + `eval/run_eval.py`, que corre el pipeline real y reporta
accuracy de veredicto, precision/recall/F1 de FAIL y matriz de confusión de
severidad. Es la herramienta para decidir con datos cualquier cambio de modelo,
prompt o perfil (a diferencia de `pytest`, sí hace llamadas reales a la API).
Cada imagen se evalúa con `debounce_frames=1`. Ampliar el set = dejar la imagen
en `eval/images/` y agregar su entrada en `labels.yaml`.

Teclas en vivo (ventana OpenCV): `SPACE` análisis manual · `Q` salir + reporte · `R` reporte de la sesión actual · `P` siguiente perfil · `D` toggle detalle de defectos · `S` screenshot.

Los tests no requieren webcam ni API key real — el cliente `anthropic` se mockea con `monkeypatch` sobre `analyzer._client.messages.parse` (structured outputs; **no** `.create`). El fake devuelve un `SimpleNamespace` con `parsed_output`, `content` y `usage`.

## Estructura y flujo de datos

- `main.py` orquesta: `run_live` (cámara), `run_single_image` (--image), `run_report_only` (--report). `build_components()` arma todo desde `settings` (cada sección de `settings.yaml` mapea a un `__init__` por `**kwargs`).
- El loop de video (30fps) nunca espera a la API: `src/worker.py` (`AnalysisWorker`) corre en un thread con `queue.Queue(maxsize=1)` — si llega un frame mientras se analiza otro, se descarta. El estado compartido (`get_latest()`, `is_analyzing`) está protegido por Lock. El loop solo hace `worker.submit()` cuando `not is_analyzing` **y** `frame_selector.should_analyze()`.
- Errores de API los captura el worker (`except anthropic.APIError`), registra `last_error` y sigue; cualquier otra excepción se imprime y también se traga — el loop nunca cae.
- `VisionAnalyzer.analyze()` propaga errores de API; es el worker quien los captura para no tumbar el loop.
- `DecisionEngine` aplica tres capas sobre el veredicto crudo de Claude: degradación por confianza (FAIL < `fail_threshold` → WARN; WARN < `warn_threshold` → PASS); **enforcement de `fail_on_severity`** (si algún defecto detectado alcanza el umbral de severidad del perfil activo — orden `cosmetic < minor < major < critical` — con confianza ≥ `fail_threshold`, el veredicto se fuerza a FAIL aunque Claude haya devuelto WARN/PASS; esto compensa que modelos económicos como Haiku a veces subestiman la severidad global aunque clasifiquen bien los defectos individuales); y debounce (FAIL firme solo tras N consecutivos; defecto `critical` con confianza alta = FAIL inmediato sin debounce, el resto de severidades pasa por debounce normal). `DecisionEngine.fail_on_severity` se inicializa desde `profile["fail_on_severity"]` en `build_components()` y se actualiza con `set_fail_on_severity()` al rotar de perfil (tecla `P`).
- `Alerter` (`src/alerter.py`) dispara **solo** cuando `verdict.status == "FAIL" and verdict.is_confirmed` (o PASS si `sound_on_pass`). Todas las alertas corren en threads daemon y tragan sus excepciones — una alerta fallida nunca afecta la inspección. El webhook (`_send_webhook`) postea payload compatible Slack/Discord (`{"text", "content"}`) a `WEBHOOK_URL`.
- `Storage` comparte una conexión SQLite entre threads (`check_same_thread=False` + Lock). Dos tablas: `sessions` y `inspections` (incluye `raw_response`: el JSON crudo del veredicto final; `model`: qué modelo lo produjo; `primary_response`: el raw del modelo primario cuando hubo escalado híbrido — todo para auditoría). `_migrate()` agrega columnas nuevas a DBs de versiones previas con `ALTER TABLE` (idempotente vía `PRAGMA table_info`). Guardar el frame de evidencia depende de `storage.save_{pass,warn,fail}_frames`; va a `data/sessions/<id>/frames/`, los reportes HTML standalone a `reports/`.
- El parseo de la respuesta de Claude está aislado en `VisionAnalyzer._parse_response()` (estático, fácil de testear): limpia fences de markdown, aísla el objeto JSON, y ante cualquier fallo retorna WARN con `evaluable=False` — nunca lanza.
- El dashboard usa solo ASCII en overlays: las fuentes Hershey de cv2 no renderizan unicode (✓✗⚠ saldrían como "?").
