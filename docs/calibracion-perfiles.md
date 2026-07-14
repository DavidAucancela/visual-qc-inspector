# Guía de calibración de perfiles

Cómo escribir un perfil de inspección que dé veredictos confiables, y cómo
ajustarlo con datos cuando se equivoca. No hay reglas de visión en código: la
precisión de un producto depende casi por completo de su perfil YAML
(`config/profiles/<producto>.yaml`) y del modelo activo.

## Anatomía de un perfil

Campos que el código usa:

- `name`, `description`, `version` — identidad del perfil.
- `inspection_criteria` — texto natural que va al prompt. Qué mirar y qué
  cuenta como defecto para *este* producto.
- `context` — contexto adicional; acá va la **rúbrica de severidad** (ver abajo).
- `fail_on_severity` — umbral que dispara FAIL (`cosmetic < minor < major < critical`).
  El `DecisionEngine` fuerza FAIL si algún defecto alcanza este nivel con
  confianza suficiente, aunque el modelo devuelva WARN/PASS.

## Escribir una rúbrica de severidad efectiva

El modelo por defecto (Haiku 4.5, económico) tiende a **subestimar** la
severidad global. La mitigación es una rúbrica explícita en `context` con
ejemplos concretos por dominio y la regla *"ante la duda, clasifica en el nivel
más severo"*. Patrón (ver `config/profiles/pcb.yaml`):

```yaml
fail_on_severity: minor
context: |
  Rúbrica de severidad (ante la duda, el nivel más severo):
  - critical: <defecto que hace el producto inservible o peligroso>
  - major:    <defecto funcional claro pero no catastrófico>
  - minor:    <defecto real de bajo impacto>
  - cosmetic: <variación estética sin impacto>
```

Reglas prácticas:

1. **Un ejemplo por nivel, del dominio real.** "soldadura fría" comunica mucho
   más que "defecto grave".
2. **Marcá la frontera minor/cosmetic**, que es donde más se equivoca: definí
   qué es tolerable vs. qué ya es defecto reportable.
3. **`fail_on_severity` bajo (`minor`) + rúbrica estricta** compensan que el
   modelo económico subestime, sin necesidad de subir de modelo. Si tenés
   muchos falsos positivos, subí el umbral antes de tocar la rúbrica.
4. **Mantené estable la parte del prompt que no cambia** entre frames (criterios
   y contexto): es lo que permite razonar sobre el comportamiento del modelo.

## Validar un perfil con el golden set

Nunca calibres a ojo. El golden set (`eval/`) mide la precisión con datos:

```bash
python eval/run_eval.py --profile <tu_perfil>
```

Reporta accuracy de veredicto, precision/recall/F1 de FAIL y matriz de
confusión de severidad. Flujo recomendado:

1. Etiquetá a mano 8–15 imágenes variadas del producto (buenas, límite, malas,
   cubriendo cada nivel de severidad) en `eval/images/` + `eval/labels.yaml`.
2. Corré la evaluación y leé la matriz de confusión de severidad.
3. Cambiá **una cosa a la vez** (rúbrica, umbral o modelo) y volvé a correr.
   Comparás números, no impresiones.

Para decidir modelo, corré el mismo set con `--model` (A/B):

```bash
python eval/run_eval.py --profile <tu_perfil> --model claude-sonnet-4-6
```

## Qué tocar según el error

Leé la métrica antes de decidir el ajuste:

| Síntoma | Métrica | Qué ajustar |
|---|---|---|
| **Falsos positivos** (marca FAIL productos buenos) | precision de FAIL baja | Subí `fail_on_severity` (ej. `minor`→`major`); afiná la frontera minor/cosmetic en la rúbrica; subí `decision.fail_threshold`. |
| **Falsos negativos** (deja pasar defectos) | recall de FAIL bajo | Bajá `fail_on_severity`; agregá el defecto que se escapa como ejemplo en la rúbrica; reforzá `inspection_criteria`; evaluá escalado híbrido (`api.escalation_model`) o subir de modelo. |
| **Severidad mal clasificada pero veredicto ok** | matriz de confusión desplazada | Ejemplos más precisos por nivel en la rúbrica; la regla "ante la duda, el más severo". |
| **En vivo tarda en confirmar un FAIL** | — | Bajá `decision.debounce_frames` (menos frames para confirmar); recordá que `critical` con confianza alta es FAIL inmediato. |

## Escalado híbrido cuando la precisión no alcanza

Si con Haiku no llegás a la precisión que necesitás pero el costo importa,
activá el escalado: Haiku analiza todos los frames y solo los WARN/FAIL se
re-confirman con un modelo caro.

```yaml
api:
  escalation_model: claude-sonnet-4-6   # vacío = desactivado
  escalate_on: [WARN, FAIL]
```

Medí el impacto con el golden set **antes y después** para confirmar que la
mejora justifica el costo extra.
