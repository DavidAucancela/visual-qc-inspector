"""Evaluación del pipeline contra un golden set etiquetado (eval/labels.yaml).

Corre el pipeline real (Preprocessor → VisionAnalyzer → DecisionEngine) sobre
cada imagen etiquetada y reporta:
  - Accuracy de veredicto (PASS/WARN/FAIL final vs esperado)
  - Precision / Recall / F1 tratando FAIL como la clase positiva
  - Matriz de confusión de severidad máxima detectada vs esperada

Debounce: en un golden set cada imagen es independiente, así que se evalúa con
`debounce_frames=1` — así el enforcement de `fail_on_severity` produce el
veredicto "intencionado" para ese frame sin la degradación temporal a WARN.

Uso:
    python eval/run_eval.py                       # modelo de settings.yaml
    python eval/run_eval.py --model claude-sonnet-4-6   # override para A/B
    python eval/run_eval.py --profile pcb         # perfil por defecto del set

Requiere ANTHROPIC_API_KEY (vía .env o entorno) — hace llamadas reales a la API.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import cv2
import yaml
from dotenv import load_dotenv

# Permitir importar src/ y main.py al correr desde la raíz del repo
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.analyzer import VisionAnalyzer  # noqa: E402
from src.decision import SEVERITY_ORDER, DecisionEngine  # noqa: E402
from src.preprocessor import Preprocessor  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
LABELS_PATH = EVAL_DIR / "labels.yaml"
CONFIG_PATH = BASE_DIR / "config" / "settings.yaml"
PROFILES_DIR = BASE_DIR / "config" / "profiles"

# Severidades para la matriz de confusión ("none" = sin defectos)
SEVERITY_LABELS = ["none", *SEVERITY_ORDER]  # none, cosmetic, minor, major, critical


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _max_severity(defects: list) -> str:
    """Severidad más alta entre los defectos detectados, o 'none' si no hay."""
    if not defects:
        return "none"
    return max(defects, key=lambda d: SEVERITY_ORDER.index(d.severity)).severity


def _pct(num: int, den: int) -> str:
    return f"{(100 * num / den):.1f}%" if den else "n/a"


def evaluate(model_override: str | None, default_profile: str) -> int:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("Falta ANTHROPIC_API_KEY (crear .env a partir de .env.example)")

    settings = _load_yaml(CONFIG_PATH)
    labels = _load_yaml(LABELS_PATH).get("samples", [])
    if not labels:
        sys.exit("eval/labels.yaml no tiene entradas en 'samples'.")

    model = model_override or settings.get("api", {}).get("model")
    preprocessor = Preprocessor(**settings["preprocessing"])
    profile_cache: dict[str, dict] = {}

    print(f"Evaluando {len(labels)} imágenes con modelo '{model}'...\n")

    verdict_ok = 0
    # FAIL como clase positiva
    tp = fp = fn = tn = 0
    # confusión de severidad: (esperada, detectada) -> conteo
    confusion: dict[tuple[str, str], int] = {}
    errors: list[str] = []

    for entry in labels:
        img_path = EVAL_DIR / entry["image"]
        prof_name = entry.get("profile", default_profile)
        exp_verdict = entry["expected_verdict"].upper()
        exp_sev = entry.get("expected_severity", "none")

        frame = cv2.imread(str(img_path))
        if frame is None:
            errors.append(f"No se pudo leer {img_path}")
            continue

        if prof_name not in profile_cache:
            profile_cache[prof_name] = _load_yaml(PROFILES_DIR / f"{prof_name}.yaml")
        profile = profile_cache[prof_name]

        analyzer = VisionAnalyzer(api_key=api_key, profile=profile, model=model)
        # debounce_frames=1: cada imagen es independiente
        decision = DecisionEngine(
            **{**settings["decision"], "debounce_frames": 1},
            fail_on_severity=profile.get("fail_on_severity", "major"),
        )

        try:
            result = analyzer.analyze(preprocessor.to_base64(preprocessor.process(frame)))
        except Exception as exc:  # noqa: BLE001 - reportar y seguir con el resto
            errors.append(f"{entry['image']}: error de API ({exc.__class__.__name__})")
            continue

        verdict = decision.evaluate(result).status
        det_sev = _max_severity(result.defects)

        # Veredicto
        if verdict == exp_verdict:
            verdict_ok += 1
        # FAIL como positivo
        exp_fail, got_fail = exp_verdict == "FAIL", verdict == "FAIL"
        if got_fail and exp_fail:
            tp += 1
        elif got_fail and not exp_fail:
            fp += 1
        elif not got_fail and exp_fail:
            fn += 1
        else:
            tn += 1
        # Severidad
        confusion[(exp_sev, det_sev)] = confusion.get((exp_sev, det_sev), 0) + 1

        flag = "OK " if verdict == exp_verdict else "XX "
        print(
            f"  {flag}{entry['image']:<28} esperado={exp_verdict:<4} "
            f"obtenido={verdict:<4} | sev esp={exp_sev:<8} det={det_sev}"
        )

    evaluated = tp + fp + fn + tn
    if evaluated == 0:
        print("\nNo se evaluó ninguna imagen.")
        for e in errors:
            print(f"  ! {e}")
        return 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    print(f"\n{'=' * 60}")
    print(f"Veredicto: {verdict_ok}/{evaluated} correctos ({_pct(verdict_ok, evaluated)})")
    print("\nFAIL como clase positiva:")
    print(f"  TP={tp}  FP={fp}  FN={fn}  TN={tn}")
    print(f"  Precision={precision:.2%}  Recall={recall:.2%}  F1={f1:.2%}")

    print("\nMatriz de confusión de severidad (fila=esperada, col=detectada):")
    header = "  " + " " * 10 + "".join(f"{s[:8]:>10}" for s in SEVERITY_LABELS)
    print(header)
    for exp in SEVERITY_LABELS:
        row = "".join(f"{confusion.get((exp, det), 0):>10}" for det in SEVERITY_LABELS)
        print(f"  {exp:<10}{row}")

    if errors:
        print(f"\nErrores ({len(errors)}):")
        for e in errors:
            print(f"  ! {e}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluar el pipeline contra el golden set")
    parser.add_argument("--model", help="override del modelo (para A/B)")
    parser.add_argument("--profile", default="generic", help="perfil por defecto del set")
    args = parser.parse_args()
    sys.exit(evaluate(args.model, args.profile))


if __name__ == "__main__":
    main()
