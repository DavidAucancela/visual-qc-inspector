# Visual QC Inspector

[![CI](https://github.com/DavidAucancela/visual-qc-inspector/actions/workflows/ci.yml/badge.svg)](https://github.com/DavidAucancela/visual-qc-inspector/actions/workflows/ci.yml)

Sistema de inspección visual de calidad en tiempo real usando webcam + Claude Vision.
La cámara captura frames, Claude analiza la imagen contra criterios escritos en lenguaje
natural, y el sistema emite veredictos **PASS / WARN / FAIL** con evidencia auditable
(frames guardados + SQLite + reporte HTML).

**Lo que lo hace diferente:** no hay reglas de visión en código. Los criterios de
inspección son texto en perfiles YAML — cualquier persona puede agregar un producto
nuevo sin tocar Python.

## Casos de uso

- Inspección de PCBs (soldaduras, componentes faltantes, daños)
- Control de packaging (etiquetas, códigos de barras, daños de caja)
- Cualquier producto con defectos visualmente identificables

## Instalación

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # agregar tu ANTHROPIC_API_KEY
```

## Inicio rápido

```bash
python main.py --profile generic     # inspección en vivo con webcam
python main.py --image foto.jpg      # analizar una sola imagen (sin cámara)
python main.py --report              # regenerar reporte de la última sesión
python main.py --export out.csv      # exportar inspecciones a CSV (--session N opcional)
python main.py --list-cameras        # listar cámaras disponibles y sus índices
python main.py --device 1            # forzar un índice de cámara puntual
```

> **macOS — se abre la cámara del iPhone en vez de la webcam:** es la Continuity
> Camera, que suele tomar el índice 0. La webcam integrada queda en el 1 (ya es
> el default en `config/settings.yaml`). Si aún abre la equivocada, corré
> `python main.py --list-cameras` y ajustá `camera.device_id` o usá `--device N`.

## Teclas durante la inspección

| Tecla | Acción |
|-------|--------|
| `SPACE` | Disparar análisis manual (en cualquier modo) |
| `Q` | Salir y generar reporte final |
| `R` | Generar reporte de la sesión actual |
| `P` | Cambiar al siguiente perfil disponible |
| `D` | Mostrar/ocultar panel de defectos |
| `S` | Screenshot del frame actual |

## Crear un perfil propio

Crear `config/profiles/mi_producto.yaml`:

```yaml
name: Botellas de vidrio
version: "1.0"
description: Línea de envasado

inspection_criteria: |
  Inspecciona la botella de vidrio buscando:
  - Grietas o fisuras en el cuerpo o cuello
  - Tapa mal cerrada o ausente
  - Nivel de llenado fuera del rango visible esperado
  - Etiqueta despegada, torcida o ilegible

fail_on_severity: minor
context: |
  Línea de envasado de bebidas. La botella debe estar íntegra
  y correctamente sellada antes de empacarse.

  Rúbrica de severidad (aplícala de forma estricta y literal):
  - critical: botella rota o tapa completamente ausente.
  - major: grieta o fisura visible, tapa mal cerrada, nivel de llenado
    claramente fuera de rango.
  - minor: etiqueta ligeramente torcida sin afectar legibilidad.
  - cosmetic: variación mínima sin impacto en integridad ni presentación.

  Ante la duda entre dos niveles, clasifica siempre en el nivel más alto.
```

Y usarlo: `python main.py --profile mi_producto` (o cambiar `active_profile`
en `config/settings.yaml`).

## Configuración (`config/settings.yaml`)

- **`api.model`** — modelo de Claude Vision (default `claude-haiku-4-5`).
  Cambiarlo ajusta también el costo estimado del dashboard si el modelo está en
  `MODEL_PRICES` (`src/analyzer.py`).
- **`api.escalation_model`** — escalado híbrido (vacío = desactivado). Si el
  modelo primario (barato) devuelve un veredicto en `api.escalate_on`
  (default `[WARN, FAIL]`), se re-consulta el mismo frame con este modelo más
  capaz y su veredicto manda. Como los PASS son mayoría, el costo extra es
  marginal y se recupera precisión donde importa. Ej.:
  `escalation_model: claude-sonnet-4-6`.
- **`frame_selector.mode`** — cuándo analizar: `timer` (cada N segundos), `diff`
  (cuando el frame cambia más que un umbral) o `manual` (solo con SPACE).
- **`decision.debounce_frames`** — un FAIL solo se confirma tras N análisis
  consecutivos en FAIL (evita falsos positivos por un frame malo). Excepción:
  defectos `critical` con confianza alta fallan de inmediato.
- **`fail_on_severity` del perfil activo** — no es solo una instrucción en el
  prompt: `DecisionEngine` la aplica en código. Si cualquier defecto detectado
  alcanza ese umbral de severidad (`cosmetic < minor < major < critical`) con
  confianza suficiente, el veredicto se fuerza a FAIL aunque Claude haya
  devuelto WARN/PASS. Esto importa especialmente con modelos económicos como
  Haiku, que a veces subestiman la severidad global aunque detecten bien los
  defectos individuales.
- **`roi.enabled`** — al iniciar, seleccionar interactivamente la región de
  interés; solo esa zona se envía a análisis (menos tokens, más precisión).
- **`storage.save_pass_frames`** — desactivado por defecto para no llenar disco.

## Reportes

Al salir (`Q`) o con `R` se genera un HTML standalone en `reports/`:
resumen de sesión (totales y porcentajes), gráfico de torta, tabla sorteable de
inspecciones, y miniaturas embebidas de los frames de FAIL. No requiere servidor
ni conexión: se abre directo en el navegador y se puede archivar o enviar.

La evidencia cruda queda en `data/`: SQLite (`qc.db`) con todas las inspecciones,
y frames JPEG en `data/sessions/<id>/frames/`.

## Costo de API y rendimiento

Estimación con Claude Haiku (`claude-haiku-4-5`, $1/M tokens entrada, $5/M salida):

- Un frame JPEG de 1024px ≈ 150–300 KB → ~200–400 tokens de imagen
- Con `timer_interval_sec: 3` → ~20 análisis/minuto → ~8000 tokens/minuto
- **Aproximadamente $0.0007–0.001 USD por análisis** (el dashboard muestra el
  costo acumulado real de la sesión usando los tokens reportados por la API)

Optimizaciones ya implementadas:

- `jpeg_quality: 85` reduce tokens sin perder detalle relevante
- El `frame_selector` evita enviar frames sin cambios (modo `diff`) o limita
  la frecuencia (modo `timer`)
- `debounce_frames` reduce alertas redundantes de defectos ya confirmados
- El worker descarta frames si ya hay un análisis en curso (nunca se encolan
  frames viejos)

## Limitaciones

- La latencia de análisis es de 2–4 segundos: esto **no** es inspección a
  velocidad de línea industrial; es apropiado para inspección asistida,
  muestreo o estaciones de trabajo manuales.
- La calidad del veredicto depende de la iluminación y el enfoque. Si la imagen
  no es evaluable, el sistema reporta WARN con `evaluable: false` en lugar de
  adivinar.
- Claude Vision no reemplaza una certificación metrológica: úsalo como filtro
  inteligente y registro auditable, con revisión humana de los FAIL.
- El proyecto usa `claude-haiku-4-5` (el modelo más económico de Claude) para
  minimizar costo; a cambio, es algo menos preciso clasificando severidad que
  Sonnet. Se compensa con `fail_on_severity` estricto (`minor`), rúbricas de
  severidad explícitas en cada perfil y enforcement en `DecisionEngine` (ver
  Configuración arriba). Si en producción se detectan demasiados falsos
  negativos, subir a `claude-sonnet-4-6`/`claude-sonnet-5` cambiando
  `api.model` en `config/settings.yaml` es la palanca más directa (el costo
  del dashboard se ajusta solo vía `MODEL_PRICES` en `src/analyzer.py`).

## Tests

```bash
pytest                                      # todos
pytest tests/test_decision.py -v            # un archivo
pytest tests/test_analyzer.py::test_parse_valid_json_response  # un test
```

Los tests no requieren webcam ni API key real: el cliente de Anthropic se mockea.
