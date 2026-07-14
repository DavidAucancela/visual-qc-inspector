# Changelog

## v2 (en progreso) — Fase 2: robustez operativa

### 2.4 Reconexión de cámara
- `CameraCapture.try_reopen()`: reintenta abrir el dispositivo sin lanzar (libera el
  handle previo y valida con una lectura real; abre-pero-no-captura cuenta como fallo).
- En vivo, si `cam.read()` devuelve `None`, `run_live` ya no termina: `_reconnect_camera()`
  muestra "CAMARA DESCONECTADA" en la ventana, reintenta con backoff (0.5→5 s) y sigue
  capturando al reconectar. Q sale durante la desconexión.

### 2.3 Retención de evidencia (`storage.retention_days`)
- Nuevo `retention_days` en `settings.yaml` (default 0 = nunca borra). Al construir
  `Storage` (arranque de cualquier modo), `_apply_retention()` borra las sesiones cuyo
  `started_at` sea más viejo que N días: filas de `sessions` + `inspections` y la
  carpeta `data/sessions/<id>/`. Borrado de disco best-effort
  (`shutil.rmtree(ignore_errors=True)`) — un fallo de FS no aborta el arranque. Evita
  que `data/sessions/` crezca sin límite en uso continuo.

### 2.1 Modo batch (`--dir carpeta/`)
- `run_batch()` en `main.py`: analiza todas las imágenes de una carpeta
  (`.jpg/.jpeg/.png/.bmp/.webp`) en **una sola sesión** y genera **un solo reporte**.
  Reutiliza el pipeline de `--image` (preprocess → analyze → decisión → storage).
  Cada imagen es independiente (`decision.reset()` entre una y otra, sin arrastrar
  debounce — es análisis offline, no stream). Imprime PASS/WARN/FAIL por imagen y un
  resumen con costo estimado al final. Sale con error si la carpeta no existe o no
  tiene imágenes.

### 2.2 Export CSV (`--export archivo.csv [--session N]`)
- `run_export()` en `main.py`: vuelca las inspecciones de una sesión (la última si no
  se pasa `--session`) a CSV con `csv.DictWriter`. Columnas: timestamp, verdict,
  confidence, summary, defects, latency_ms, model, frame_path. Los defectos se
  serializan como `[severidad] descripción (confianza%)` separados por `; `.
- Sin tocar el schema SQLite (solo lectura vía `get_inspections`). Sale con error si
  no hay sesiones o la sesión no tiene inspecciones (no crea CSV vacío).

### 3.3 CI con GitHub Actions
- `.github/workflows/ci.yml`: corre `pytest` en push/PR sobre Python 3.10 y 3.12
  (matriz). Los tests mockean API y cámara — no requieren secrets. Badge en el README.

### Verificación
- 51/51 tests pasan (10 nuevos: batch en una sesión + carpeta vacía; export escribe
  CSV con defectos + sale sin sesiones; retención borra sesiones viejas + retención 0
  conserva todo; try_reopen éxito/no-abre/abre-sin-frame/libera-handle-previo).

## v2 — Fase 1: precisión medible

Ver `ROADMAP.md` para el plan completo. Fase 1 entregada:

### 1.3 Modelo configurable (`api.model` en settings.yaml)
- `MODEL` deja de estar hardcodeada: `VisionAnalyzer(model=...)` la recibe desde
  `settings.yaml` vía `build_components()`. Default sigue siendo `claude-haiku-4-5`.
- `MODEL_PRICES` (dict modelo→(input,output) USD/1M tokens) alimenta el costo del
  dashboard; modelo no listado usa fallback de Haiku sin romper.

### 1.1 Golden set de evaluación (`eval/`)
- `eval/labels.yaml` (imágenes etiquetadas a mano) + `eval/run_eval.py`: corre el
  pipeline real y reporta accuracy de veredicto, precision/recall/F1 de FAIL y
  matriz de confusión de severidad. Herramienta para decidir con datos cualquier
  cambio de modelo/prompt/perfil y para A/B de modelos (`--model`).
- Cada imagen se evalúa con `debounce_frames=1` (independiente). Set inicial
  mínimo (`test_sample.jpg`); se amplía dejando imágenes en `eval/images/`.

### 1.2 Escalado híbrido (Haiku → modelo caro)
- `api.escalation_model` (vacío = off) + `api.escalate_on` (default `[WARN, FAIL]`)
  en settings. Si el modelo primario devuelve WARN/FAIL, `analyze()` re-consulta
  el frame con el modelo caro y su veredicto reemplaza al primario.
- Costo acumulado ahora por llamada con el precio de cada modelo
  (`total_cost_usd`), correcto al mezclar modelos.
- Auditoría: `InspectionResult` gana `model`, `escalated`, `primary_raw_response`;
  SQLite gana columnas `model` y `primary_response` (con migración idempotente).

### Verificación
- 41/41 tests pasan (5 nuevos: modelo configurable + precios fallback, y 3 del
  escalado: reemplazo de veredicto, no-escala en PASS, desactivado por default).

## v1 — 2026-07-07

### Cambio de modelo: `claude-sonnet-4-6` → `claude-haiku-4-5`

Decisión de costo: Haiku es ~3x más barato que Sonnet 4.6 en tokens de entrada y salida
($1/$5 por 1M vs $3/$15 por 1M). Se evaluó y se descartó migrar a un proveedor externo
(OpenAI `gpt-4.1-nano`) — se probó la integración completa (analyzer, worker, tests,
docs) pero se revirtió por decisión del proyecto de mantenerse en el ecosistema Claude.

- `src/analyzer.py`: `MODEL = "claude-haiku-4-5"`, precios actualizados a
  `INPUT_TOKEN_PRICE = 1.00 / 1_000_000` / `OUTPUT_TOKEN_PRICE = 5.00 / 1_000_000`.
- Costo estimado por análisis bajó de ~$0.002–0.003 a ~$0.0007–0.001 USD.

### Mitigación de precisión (Haiku subestima severidad más que Sonnet)

Al bajar de modelo se detectó que Haiku a veces clasifica correctamente los defectos
individuales pero subestima el veredicto global (ej. defecto `major` reportado como
veredicto `WARN`). Se aplicaron tres mitigaciones:

1. **`fail_on_severity: minor`** en los 3 perfiles (`generic`, `packaging`, `pcb`),
   antes `major` — más estricto, exige menos severidad para gatillar FAIL.
2. **Rúbricas de severidad explícitas** agregadas al campo `context` de cada perfil:
   definen con ejemplos concretos qué es `critical`/`major`/`minor`/`cosmetic` para
   ese dominio, con la regla "ante la duda, clasifica en el nivel más severo".
3. **Enforcement programático en `DecisionEngine`** (`src/decision.py`): antes el
   `fail_on_severity` del perfil era solo una instrucción en el prompt — no había
   nada en código que verificara que Claude la respetara. Ahora `DecisionEngine`
   compara cada defecto detectado contra el umbral `fail_on_severity` del perfil
   activo (orden `cosmetic < minor < major < critical`); si algún defecto lo alcanza
   con confianza ≥ `fail_threshold`, el veredicto se fuerza a FAIL aunque el modelo
   haya devuelto WARN/PASS. Sigue pasando por debounce normal (solo `critical` es
   FAIL inmediato sin debounce, sin cambios ahí).
   - Nuevo parámetro `fail_on_severity` en `DecisionEngine.__init__` (default
     `"major"`, compatible con el comportamiento previo si no se pasa).
   - Nuevo método `set_fail_on_severity()`.
   - `main.py`: `build_components()` inicializa el umbral desde el perfil activo;
     al rotar de perfil con la tecla `P` se actualiza junto con `decision.reset()`.

### Verificación

- 36/36 tests pasan. Se agregaron 4 tests nuevos en `tests/test_decision.py` para el
  enforcement de `fail_on_severity`: defecto `major` que fuerza FAIL tras debounce,
  defecto `major` que NO fuerza FAIL cuando el umbral del perfil es `critical`,
  defecto de baja confianza que no dispara el enforcement, y `set_fail_on_severity()`
  actualizando el umbral en caliente.
- Probado end-to-end con `python main.py --image test_sample.jpg` contra la API real.
