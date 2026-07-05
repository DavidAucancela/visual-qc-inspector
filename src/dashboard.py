"""Dashboard en vivo: overlay de estado, defectos y controles sobre el frame.

Nota: cv2.putText usa fuentes Hershey que no soportan unicode, por eso los
indicadores son ASCII ("PASS", "WARN [!]", "FAIL [X]") en vez de checks/cruces.
"""

from __future__ import annotations

import cv2
import numpy as np

from src.analyzer import InspectionResult
from src.decision import FinalVerdict

# BGR
GREEN = (80, 200, 80)
ORANGE = (0, 160, 255)
RED = (60, 60, 230)
GRAY = (180, 180, 180)
WHITE = (240, 240, 240)

STATUS_STYLE = {
    "PASS": ("PASS", GREEN),
    "WARN": ("WARN [!]", ORANGE),
    "FAIL": ("FAIL [X]", RED),
}

SEVERITY_COLOR = {
    "critical": RED,
    "major": RED,
    "minor": ORANGE,
    "cosmetic": GRAY,
}


class Dashboard:
    def __init__(self, settings: dict):
        self.settings = settings
        self._frame_count = 0  # para animar el spinner

    def render(
        self,
        frame: np.ndarray,
        verdict: FinalVerdict | None,
        result: InspectionResult | None,
        fps: float,
        *,
        is_analyzing: bool = False,
        profile_name: str = "",
        analysis_count: int = 0,
        est_cost_usd: float = 0.0,
        show_defects: bool = True,
    ) -> np.ndarray:
        self._frame_count += 1
        display = frame.copy()

        self._draw_status_panel(display, verdict, result, fps, analysis_count, est_cost_usd)
        if show_defects and result is not None and result.defects:
            self._draw_defects_panel(display, result)
        self._draw_bottom_bar(display, profile_name)
        if is_analyzing:
            self._draw_spinner(display)
        return display

    def _draw_status_panel(self, img, verdict, result, fps, analysis_count, est_cost_usd):
        h, w = img.shape[:2]
        panel_w, panel_h = 360, 130
        self._overlay_rect(img, (10, 10), (10 + panel_w, 10 + panel_h))

        if verdict is None:
            cv2.putText(img, "ESPERANDO...", (25, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.1, GRAY, 2, cv2.LINE_AA)
        else:
            label, color = STATUS_STYLE.get(verdict.status, ("?", GRAY))
            if verdict.status == "FAIL" and not verdict.is_confirmed:
                label += " (sin confirmar)"
            cv2.putText(img, label, (25, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.4, color, 3, cv2.LINE_AA)
            if result is not None and result.summary:
                cv2.putText(img, result.summary[:42], (25, 90),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, WHITE, 1, cv2.LINE_AA)

        latency = result.latency_ms if result else 0
        info = f"FPS {fps:.0f} | {latency} ms | {analysis_count} analisis | ~${est_cost_usd:.4f}"
        cv2.putText(img, info, (25, 122),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, GRAY, 1, cv2.LINE_AA)

    def _draw_defects_panel(self, img, result: InspectionResult):
        h, w = img.shape[:2]
        panel_w = 380
        x0 = w - panel_w - 10
        line_h = 38
        panel_h = 40 + line_h * min(len(result.defects), 6)
        self._overlay_rect(img, (x0, 10), (x0 + panel_w, 10 + panel_h))

        cv2.putText(img, f"DEFECTOS ({len(result.defects)})", (x0 + 14, 38),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 2, cv2.LINE_AA)
        for i, d in enumerate(result.defects[:6]):
            y = 70 + i * line_h
            color = SEVERITY_COLOR.get(d.severity, GRAY)
            cv2.putText(img, f"[{d.severity}] {d.confidence:.0%}", (x0 + 14, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.48, color, 1, cv2.LINE_AA)
            cv2.putText(img, d.description[:48], (x0 + 14, y + 17),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, WHITE, 1, cv2.LINE_AA)

    def _draw_bottom_bar(self, img, profile_name: str):
        h, w = img.shape[:2]
        self._overlay_rect(img, (0, h - 34), (w, h), alpha=0.7)
        keys = "SPACE: analizar | Q: salir | R: reporte | P: perfil | D: defectos | S: screenshot"
        cv2.putText(img, keys, (12, h - 11),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, WHITE, 1, cv2.LINE_AA)
        if profile_name:
            text = f"Perfil: {profile_name}"
            size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)[0]
            cv2.putText(img, text, (w - size[0] - 12, h - 11),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, ORANGE, 1, cv2.LINE_AA)

    def _draw_spinner(self, img):
        """Arco giratorio en la esquina superior derecha del panel de estado."""
        center = (392, 40)
        angle = (self._frame_count * 12) % 360
        cv2.ellipse(img, center, (14, 14), 0, angle, angle + 270, ORANGE, 3, cv2.LINE_AA)
        cv2.putText(img, "analizando...", (375, 75),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, ORANGE, 1, cv2.LINE_AA)

    @staticmethod
    def _overlay_rect(img, pt1, pt2, alpha: float = 0.55):
        """Rectángulo negro semitransparente."""
        x1, y1 = pt1
        x2, y2 = pt2
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(img.shape[1], x2), min(img.shape[0], y2)
        if x2 <= x1 or y2 <= y1:
            return
        sub = img[y1:y2, x1:x2]
        black = np.zeros_like(sub)
        img[y1:y2, x1:x2] = cv2.addWeighted(sub, 1 - alpha, black, alpha, 0)
