# Golden set de evaluación

Mide objetivamente la precisión del pipeline de inspección, para poder decidir
con datos (no a ojo) cualquier cambio de modelo, prompt o perfil.

## Uso

```bash
python eval/run_eval.py                          # modelo de config/settings.yaml
python eval/run_eval.py --model claude-sonnet-4-6   # override para A/B
python eval/run_eval.py --profile pcb            # perfil por defecto del set
```

Requiere `ANTHROPIC_API_KEY` (hace llamadas reales a la API).

## Qué reporta

- **Accuracy de veredicto**: PASS/WARN/FAIL final vs esperado.
- **Precision / Recall / F1** tratando FAIL como clase positiva — recall bajo =
  falsos negativos (defectos que se escapan), precision baja = falsos positivos.
- **Matriz de confusión de severidad**: severidad máxima detectada vs esperada,
  útil para ver si un modelo económico subestima (ej. clasifica `major` como
  `minor`).

## Ampliar el set

1. Dejá la imagen en `eval/images/`.
2. Agregá su entrada en `eval/labels.yaml` con el veredicto y la severidad
   máxima esperados (etiquetados a mano).

Cuantas más imágenes y más variadas (buenas, límite, malas, cubriendo cada
nivel de severidad), más confiable la métrica. Con un set chico, tratá los
números como orientativos.

## Notas

- Cada imagen se evalúa con `debounce_frames=1` (independiente): así el
  enforcement de `fail_on_severity` da el veredicto "intencionado" del frame
  sin la degradación temporal a WARN que aplica el debounce en vivo.
- El set versionado es intencionalmente mínimo (`test_sample.jpg`). Ampliarlo
  con evidencia real es parte del trabajo continuo de calibración.
