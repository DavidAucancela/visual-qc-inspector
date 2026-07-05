"""Visual QC Inspector — inspección visual de calidad con Claude Vision.

Uso:
  python main.py                        # perfil de config/settings.yaml
  python main.py --profile pcb          # perfil específico
  python main.py --image path/img.jpg   # analizar una sola imagen (sin cámara)
  python main.py --report               # solo generar reporte de la última sesión

Teclas en vivo:
  SPACE → disparar análisis manual    R → generar reporte de sesión actual
  Q     → salir y generar reporte     P → cambiar al siguiente perfil
  D     → toggle detalle de defectos  S → screenshot del frame actual
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import yaml
from dotenv import load_dotenv

from src.alerter import Alerter
from src.analyzer import VisionAnalyzer
from src.capture import CameraCapture
from src.dashboard import Dashboard
from src.decision import DecisionEngine
from src.frame_selector import FrameSelector
from src.preprocessor import Preprocessor
from src.reporter import Reporter
from src.storage import Storage
from src.worker import AnalysisWorker

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config" / "settings.yaml"
PROFILES_DIR = BASE_DIR / "config" / "profiles"
DB_PATH = BASE_DIR / "data" / "qc.db"
SESSIONS_DIR = BASE_DIR / "data" / "sessions"


def load_settings() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_profile(name: str) -> dict:
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        available = ", ".join(list_profiles())
        sys.exit(f"Perfil '{name}' no existe. Disponibles: {available}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))


def build_components(settings: dict, profile: dict, api_key: str):
    preprocessor = Preprocessor(**settings["preprocessing"])
    frame_selector = FrameSelector(**settings["frame_selector"])
    analyzer = VisionAnalyzer(
        api_key=api_key,
        profile=profile,
        max_retries=settings.get("api", {}).get("max_retries", 3),
        observatory_url=os.environ.get("OBSERVATORY_URL", ""),
        observatory_token=os.environ.get("OBSERVATORY_TOKEN", ""),
    )
    decision = DecisionEngine(**settings["decision"])
    storage = Storage(str(DB_PATH), str(SESSIONS_DIR), settings.get("storage", {}))
    alerter = Alerter(settings, webhook_url=os.environ.get("WEBHOOK_URL", ""))
    reporter = Reporter(str(BASE_DIR / "templates"), str(BASE_DIR / "reports"))
    return preprocessor, frame_selector, analyzer, decision, storage, alerter, reporter


def run_single_image(image_path: str, settings: dict, profile: dict, api_key: str) -> None:
    """Analiza una imagen estática y muestra el resultado (modo testing)."""
    frame = cv2.imread(image_path)
    if frame is None:
        sys.exit(f"No se pudo leer la imagen: {image_path}")

    preprocessor, _, analyzer, decision, storage, _, reporter = build_components(
        settings, profile, api_key
    )
    session_id = storage.start_session(profile["name"])

    processed = preprocessor.process(frame)
    print(f"Analizando {image_path} con perfil '{profile['name']}'...")
    result = analyzer.analyze(preprocessor.to_base64(processed))
    verdict = decision.evaluate(result)
    storage.save_inspection(session_id, verdict, result, processed)
    storage.end_session(session_id)

    print(f"\nVeredicto: {verdict.status} (confianza {result.overall_confidence:.0%})")
    print(f"Resumen:   {result.summary}")
    print(f"Latencia:  {result.latency_ms} ms | "
          f"tokens in/out: {result.input_tokens}/{result.output_tokens}")
    for d in result.defects:
        print(f"  - [{d.severity}] {d.description} — {d.location} ({d.confidence:.0%})")

    path = reporter.generate(session_id, storage)
    print(f"\nReporte: {path}")
    storage.close()


def run_report_only() -> None:
    storage = Storage(str(DB_PATH), str(SESSIONS_DIR))
    session_id = storage.get_last_session_id()
    if session_id is None:
        sys.exit("No hay sesiones registradas todavía.")
    reporter = Reporter(str(BASE_DIR / "templates"), str(BASE_DIR / "reports"))
    path = reporter.generate(session_id, storage)
    print(f"Reporte de la sesión {session_id}: {path}")
    storage.close()


def run_live(settings: dict, profile_name: str, api_key: str) -> None:
    profile = load_profile(profile_name)
    (preprocessor, frame_selector, analyzer, decision,
     storage, alerter, reporter) = build_components(settings, profile, api_key)

    cam_cfg = settings["camera"]
    roi_cfg = settings.get("roi", {})
    dashboard = Dashboard(settings)
    show_defects = True
    fps, fps_t0, fps_frames = 0.0, time.monotonic(), 0

    with CameraCapture(cam_cfg["device_id"], cam_cfg["width"], cam_cfg["height"]) as cam:
        # ROI: selección interactiva al inicio; valores de config como fallback
        roi = None
        if roi_cfg.get("enabled", False):
            first = cam.read()
            if first is not None:
                roi = cam.select_roi(first)
                if roi[2] == 0 or roi[3] == 0:  # cancelado → usar config
                    roi = (roi_cfg["x"], roi_cfg["y"], roi_cfg["width"], roi_cfg["height"])
                print(f"ROI activo: {roi}")

        session_id = storage.start_session(profile["name"])
        worker = AnalysisWorker(analyzer, decision, storage, alerter, session_id)
        worker.start()
        print(f"Sesión {session_id} iniciada — perfil '{profile['name']}' "
              f"(modo {frame_selector.mode})")

        try:
            while True:
                frame = cam.read()
                if frame is None:
                    print("La cámara dejó de entregar frames.")
                    break

                if roi is not None:
                    frame = cam.crop_roi(frame, roi)

                processed = preprocessor.process(frame)

                if not worker.is_analyzing and frame_selector.should_analyze(processed):
                    worker.submit(preprocessor.to_base64(processed), processed)

                # FPS (ventana de 1 segundo)
                fps_frames += 1
                now = time.monotonic()
                if now - fps_t0 >= 1.0:
                    fps = fps_frames / (now - fps_t0)
                    fps_t0, fps_frames = now, 0

                latest = worker.get_latest()
                display = dashboard.render(
                    frame,
                    latest,
                    latest.result if latest else None,
                    fps,
                    is_analyzing=worker.is_analyzing,
                    profile_name=profile["name"],
                    analysis_count=analyzer.total_analyses,
                    est_cost_usd=analyzer.estimated_cost_usd,
                    show_defects=show_defects,
                )
                cv2.imshow("QC Inspector", display)

                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                elif key == ord(" "):
                    frame_selector.trigger()
                elif key == ord("r"):
                    path = reporter.generate(session_id, storage)
                    print(f"Reporte generado: {path}")
                elif key == ord("p"):
                    profiles = list_profiles()
                    idx = (profiles.index(profile_name) + 1) % len(profiles)
                    profile_name = profiles[idx]
                    profile = load_profile(profile_name)
                    analyzer.set_profile(profile)
                    decision.reset()
                    print(f"Perfil cambiado a '{profile['name']}' "
                          f"(disponibles: {', '.join(profiles)})")
                elif key == ord("d"):
                    show_defects = not show_defects
                elif key == ord("s"):
                    shot = SESSIONS_DIR / str(session_id) / (
                        f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
                    )
                    cv2.imwrite(str(shot), frame)
                    print(f"Screenshot: {shot}")
        finally:
            worker.stop()
            worker.join(timeout=5)
            storage.end_session(session_id)
            path = reporter.generate(session_id, storage)
            print(f"Sesión finalizada. Reporte: {path}")
            print(f"Análisis: {analyzer.total_analyses} | "
                  f"costo estimado: ${analyzer.estimated_cost_usd:.4f} USD")
            storage.close()
            cv2.destroyAllWindows()


def main() -> None:
    parser = argparse.ArgumentParser(description="Visual QC Inspector")
    parser.add_argument("--profile", help="nombre del perfil (config/profiles/)")
    parser.add_argument("--no-camera", action="store_true",
                        help="no usar cámara (requiere --image)")
    parser.add_argument("--image", help="analizar una sola imagen y salir")
    parser.add_argument("--report", action="store_true",
                        help="solo generar reporte de la última sesión")
    args = parser.parse_args()

    if args.report:
        run_report_only()
        return

    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("Falta ANTHROPIC_API_KEY (crear .env a partir de .env.example)")

    settings = load_settings()
    profile_name = args.profile or settings.get("active_profile", "generic")

    if args.image:
        run_single_image(args.image, settings, load_profile(profile_name), api_key)
        return
    if args.no_camera:
        sys.exit("--no-camera requiere --image ruta/imagen.jpg")

    run_live(settings, profile_name, api_key)


if __name__ == "__main__":
    main()
