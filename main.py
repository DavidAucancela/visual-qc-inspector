"""Visual QC Inspector — inspección visual de calidad con Claude Vision.

Uso:
  python main.py                        # perfil de config/settings.yaml
  python main.py --profile pcb          # perfil específico
  python main.py --image path/img.jpg   # analizar una sola imagen (sin cámara)
  python main.py --dir carpeta/         # analizar un lote de imágenes (una sesión)
  python main.py --report               # solo generar reporte de la última sesión
  python main.py --export out.csv       # exportar inspecciones a CSV (--session N opcional)

Teclas en vivo:
  SPACE → disparar análisis manual    R → generar reporte de sesión actual
  Q     → salir y generar reporte     P → cambiar al siguiente perfil
  D     → toggle detalle de defectos  S → screenshot del frame actual
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import yaml
from dotenv import load_dotenv

from src.alerter import Alerter
from src.analyzer import MODEL, VisionAnalyzer
from src.capture import CameraCapture, list_cameras
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
    """Carga config/settings.yaml completo como dict."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_profile(name: str) -> dict:
    """Carga un perfil de inspección (config/profiles/<name>.yaml); sale si no existe."""
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        available = ", ".join(list_profiles())
        sys.exit(f"Perfil '{name}' no existe. Disponibles: {available}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_profiles() -> list[str]:
    """Lista los nombres de perfiles disponibles en config/profiles/."""
    return sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))


def build_components(settings: dict, profile: dict, api_key: str):
    """Instancia y conecta todos los componentes del pipeline a partir de settings."""
    preprocessor = Preprocessor(**settings["preprocessing"])
    frame_selector = FrameSelector(**settings["frame_selector"])
    api_cfg = settings.get("api", {})
    analyzer = VisionAnalyzer(
        api_key=api_key,
        profile=profile,
        model=api_cfg.get("model", MODEL),
        max_retries=api_cfg.get("max_retries", 3),
        observatory_url=os.environ.get("OBSERVATORY_URL", ""),
        observatory_token=os.environ.get("OBSERVATORY_TOKEN", ""),
        escalation_model=api_cfg.get("escalation_model", ""),
        escalate_on=api_cfg.get("escalate_on"),
    )
    decision = DecisionEngine(
        **settings["decision"], fail_on_severity=profile.get("fail_on_severity", "major")
    )
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


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def run_batch(dir_path: str, settings: dict, profile: dict, api_key: str) -> None:
    """Analiza todas las imágenes de una carpeta en una sola sesión y reporte.

    Reutiliza el pipeline de --image (preprocess → analyze → decisión → storage)
    pero comparte una sesión para todo el lote. Cada imagen se evalúa de forma
    independiente (debounce ya no aplica: es análisis offline, no stream).
    Útil para re-procesar evidencia o validar un lote de producción sin cámara.
    """
    directory = Path(dir_path)
    if not directory.is_dir():
        sys.exit(f"No es una carpeta: {dir_path}")
    images = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not images:
        sys.exit(f"La carpeta no tiene imágenes: {dir_path}")

    preprocessor, _, analyzer, decision, storage, _, reporter = build_components(
        settings, profile, api_key
    )
    session_id = storage.start_session(profile["name"])
    print(f"Lote: {len(images)} imágenes — perfil '{profile['name']}' "
          f"(sesión {session_id})")

    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for i, img_path in enumerate(images, 1):
        frame = cv2.imread(str(img_path))
        if frame is None:
            print(f"  [{i}/{len(images)}] {img_path.name}: no se pudo leer, se omite")
            continue
        decision.reset()  # cada imagen es independiente, sin arrastrar debounce
        processed = preprocessor.process(frame)
        result = analyzer.analyze(preprocessor.to_base64(processed))
        verdict = decision.evaluate(result)
        storage.save_inspection(session_id, verdict, result, processed)
        counts[verdict.status] = counts.get(verdict.status, 0) + 1
        print(f"  [{i}/{len(images)}] {img_path.name}: {verdict.status} "
              f"(confianza {result.overall_confidence:.0%})")

    storage.end_session(session_id)
    path = reporter.generate(session_id, storage)
    print(f"\nLote completo: {counts['PASS']} PASS · {counts['WARN']} WARN · "
          f"{counts['FAIL']} FAIL")
    print(f"Costo estimado: ${analyzer.estimated_cost_usd:.4f} USD")
    print(f"Reporte: {path}")
    storage.close()


def run_list_cameras() -> None:
    """Modo --list-cameras: sondea y muestra las cámaras disponibles y sus índices."""
    print("Sondeando cámaras disponibles...\n")
    cams = list_cameras()
    if not cams:
        print("No se detectó ninguna cámara.")
        return
    for c in cams:
        frame_note = "entrega frame" if c["delivers_frame"] else "abre pero NO entrega frame"
        print(f"  index {c['index']}: {c['width']}x{c['height']} — {frame_note}")
    print(
        "\nElegí el índice de tu webcam y ponelo en config/settings.yaml "
        "(camera.device_id),\no usalo directo con:  python main.py --device N\n"
        "En macOS, la cámara del iPhone (Continuity Camera) suele ser el index 0."
    )


def run_report_only() -> None:
    """Modo --report: regenera el reporte HTML de la última sesión guardada."""
    storage = Storage(str(DB_PATH), str(SESSIONS_DIR))
    session_id = storage.get_last_session_id()
    if session_id is None:
        sys.exit("No hay sesiones registradas todavía.")
    reporter = Reporter(str(BASE_DIR / "templates"), str(BASE_DIR / "reports"))
    path = reporter.generate(session_id, storage)
    print(f"Reporte de la sesión {session_id}: {path}")
    storage.close()


def run_export(csv_path: str, session_id: int | None = None) -> None:
    """Modo --export: vuelca las inspecciones de una sesión a CSV para análisis externo.

    Sin --session usa la última sesión registrada. Cada defecto se serializa como
    "[severidad] descripción (confianza%)" separados por "; " en una sola celda.
    """
    storage = Storage(str(DB_PATH), str(SESSIONS_DIR))
    try:
        if session_id is None:
            session_id = storage.get_last_session_id()
        if session_id is None:
            sys.exit("No hay sesiones registradas todavía.")
        inspections = storage.get_inspections(session_id)
        if not inspections:
            sys.exit(f"La sesión {session_id} no tiene inspecciones.")

        fields = ["timestamp", "verdict", "confidence", "summary",
                  "defects", "latency_ms", "model", "frame_path"]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for insp in inspections:
                defects = "; ".join(
                    f"[{d.get('severity', '')}] {d.get('description', '')}"
                    f" ({d.get('confidence', 0):.0%})"
                    for d in insp.get("defects", [])
                )
                writer.writerow({
                    "timestamp": insp.get("timestamp", ""),
                    "verdict": insp.get("verdict", ""),
                    "confidence": insp.get("overall_confidence", ""),
                    "summary": insp.get("summary", ""),
                    "defects": defects,
                    "latency_ms": insp.get("latency_ms", ""),
                    "model": insp.get("model", "") or "",
                    "frame_path": insp.get("frame_path", "") or "",
                })
        print(f"Exportadas {len(inspections)} inspecciones de la sesión "
              f"{session_id} a {csv_path}")
    finally:
        storage.close()


def _reconnect_camera(cam: CameraCapture, width: int, height: int) -> bool:
    """Muestra 'CAMARA DESCONECTADA' y reintenta reabrir con backoff.

    Mantiene la ventana OpenCV viva (para poder salir con Q durante la caída).
    Retorna True cuando reconecta, False si el usuario presionó Q. El backoff
    crece 0.5→1→2→4→5 s (tope 5 s) para no martillar el dispositivo.
    """
    print("La cámara dejó de entregar frames. Reintentando reconexión...")
    screen = np.zeros((height, width, 3), dtype=np.uint8)
    delay = 0.5
    attempt = 0
    while True:
        attempt += 1
        frame = screen.copy()
        cv2.putText(frame, "CAMARA DESCONECTADA", (40, height // 2 - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
        cv2.putText(frame, f"Reintentando... (intento {attempt}) - Q para salir",
                    (40, height // 2 + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255), 2)
        cv2.imshow("QC Inspector", frame)
        # waitKey hace de sleep y a la vez atiende el teclado
        key = cv2.waitKey(int(delay * 1000)) & 0xFF
        if key == ord("q"):
            return False
        if cam.try_reopen():
            print(f"Cámara reconectada tras {attempt} intento(s).")
            cam.warmup(10)
            return True
        delay = min(delay * 2, 5.0)


def run_live(settings: dict, profile_name: str, api_key: str) -> None:
    """Loop principal en vivo: captura de cámara, dashboard OpenCV y manejo de teclas."""
    profile = load_profile(profile_name)
    (preprocessor, frame_selector, analyzer, decision,
     storage, alerter, reporter) = build_components(settings, profile, api_key)

    cam_cfg = settings["camera"]
    roi_cfg = settings.get("roi", {})
    dashboard = Dashboard(settings)
    show_defects = True
    fps, fps_t0, fps_frames = 0.0, time.monotonic(), 0

    with CameraCapture(cam_cfg["device_id"], cam_cfg["width"], cam_cfg["height"]) as cam:
        print("Calentando cámara (descartando primeros frames)...")
        cam.warmup(30)  # descarta primeros frames (cámara se estabiliza)
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
        cv2.namedWindow("QC Inspector", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("QC Inspector", 1280, 720)

        try:
            loop_frames = 0
            while True:
                frame = cam.read()
                if frame is None:
                    if _reconnect_camera(cam, cam_cfg["width"], cam_cfg["height"]):
                        continue  # reconectada: seguir capturando
                    break  # el usuario pidió salir (Q) durante la desconexión

                loop_frames += 1
                if loop_frames % 30 == 0:  # debug: cada 1 segundo (30 fps)
                    print(f"  → {loop_frames} frames capturados")

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
                    debounce_frames=decision.debounce_frames,
                    fail_on_severity=decision.fail_on_severity,
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
                    decision.set_fail_on_severity(profile.get("fail_on_severity", "major"))
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
    """Punto de entrada: parsea argumentos CLI y despacha al modo correspondiente."""
    parser = argparse.ArgumentParser(description="Visual QC Inspector")
    parser.add_argument("--profile", help="nombre del perfil (config/profiles/)")
    parser.add_argument("--no-camera", action="store_true",
                        help="no usar cámara (requiere --image)")
    parser.add_argument("--image", help="analizar una sola imagen y salir")
    parser.add_argument("--dir", help="analizar todas las imágenes de una carpeta y salir")
    parser.add_argument("--report", action="store_true",
                        help="solo generar reporte de la última sesión")
    parser.add_argument("--list-cameras", action="store_true",
                        help="listar cámaras disponibles y sus índices, y salir")
    parser.add_argument("--device", type=int,
                        help="índice de cámara a usar (override de camera.device_id)")
    parser.add_argument("--export", metavar="ARCHIVO.csv",
                        help="exportar inspecciones a CSV (usa --session o la última)")
    parser.add_argument("--session", type=int,
                        help="id de sesión para --export (default: la última)")
    args = parser.parse_args()

    if args.list_cameras:
        run_list_cameras()
        return

    if args.report:
        run_report_only()
        return

    if args.export:
        run_export(args.export, args.session)
        return

    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("Falta ANTHROPIC_API_KEY (crear .env a partir de .env.example)")

    settings = load_settings()
    profile_name = args.profile or settings.get("active_profile", "generic")
    if args.device is not None:
        settings.setdefault("camera", {})["device_id"] = args.device

    if args.image:
        run_single_image(args.image, settings, load_profile(profile_name), api_key)
        return
    if args.dir:
        run_batch(args.dir, settings, load_profile(profile_name), api_key)
        return
    if args.no_camera:
        sys.exit("--no-camera requiere --image ruta/imagen.jpg")

    run_live(settings, profile_name, api_key)


if __name__ == "__main__":
    main()
