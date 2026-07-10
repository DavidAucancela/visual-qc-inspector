"""Generación de reportes HTML standalone con jinja2."""

from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path

import cv2
from jinja2 import Environment, FileSystemLoader

from src.storage import Storage

THUMB_WIDTH = 320


class Reporter:
    def __init__(self, template_path: str = "templates", output_dir: str = "reports"):
        """Crea el directorio de reportes y configura el entorno jinja2."""
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._env = Environment(
            loader=FileSystemLoader(template_path), autoescape=True
        )

    def generate(self, session_id: int, storage: Storage) -> str:
        """Genera el reporte HTML de la sesión y retorna la ruta del archivo."""
        stats = storage.get_session_stats(session_id)
        inspections = storage.get_inspections(session_id)

        # Embeber miniaturas de los frames de FAIL (HTML autocontenido)
        for item in inspections:
            item["thumbnail_b64"] = ""
            if item["verdict"] == "FAIL" and item.get("frame_path"):
                item["thumbnail_b64"] = self._thumbnail_b64(item["frame_path"])

        duration = self._session_duration(stats)
        template = self._env.get_template("report.html")
        html = template.render(
            session=stats,
            stats=stats,
            inspections=inspections,
            duration=duration,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        out_path = (
            self.output_dir
            / f"report_session_{session_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        )
        out_path.write_text(html, encoding="utf-8")
        return str(out_path)

    @staticmethod
    def _thumbnail_b64(frame_path: str) -> str:
        """Redimensiona el frame guardado en disco y lo codifica a base64 para embeber en el HTML."""
        frame = cv2.imread(frame_path)
        if frame is None:
            return ""
        h, w = frame.shape[:2]
        if w > THUMB_WIDTH:
            frame = cv2.resize(
                frame, (THUMB_WIDTH, int(h * THUMB_WIDTH / w)), interpolation=cv2.INTER_AREA
            )
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            return ""
        return base64.standard_b64encode(buf.tobytes()).decode("utf-8")

    @staticmethod
    def _session_duration(stats: dict) -> str:
        """Formatea la duración de la sesión (started_at a ended_at) como texto legible."""
        started, ended = stats.get("started_at"), stats.get("ended_at")
        if not started:
            return "-"
        try:
            t0 = datetime.fromisoformat(started)
            t1 = datetime.fromisoformat(ended) if ended else datetime.now()
            total_sec = int((t1 - t0).total_seconds())
            minutes, seconds = divmod(total_sec, 60)
            hours, minutes = divmod(minutes, 60)
            if hours:
                return f"{hours}h {minutes}m {seconds}s"
            return f"{minutes}m {seconds}s"
        except ValueError:
            return "-"
