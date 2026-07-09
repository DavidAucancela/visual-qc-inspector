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

---

## Orden sugerido de implementación

1. **1.3** (modelo configurable) — chico, desbloquea el resto.
2. **1.1** (golden set) — sin esto no se puede medir nada de lo que sigue.
3. **1.2** (escalado híbrido) — la mejora de precisión de mayor impacto.
4. **2.x** en orden — robustez para uso continuo.
5. **3.3** (CI) en cualquier momento — es independiente.
6. **3.1, 3.2, 4.x** — según necesidad.
7. **3.4** solo si aparece el caso de uso real.
