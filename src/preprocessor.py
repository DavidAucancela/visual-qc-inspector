"""Preprocesado de frames antes de enviarlos a Claude Vision."""

from __future__ import annotations

import base64

import cv2
import numpy as np


class Preprocessor:
    def __init__(
        self,
        target_width: int = 1024,
        jpeg_quality: int = 85,
        enhance_contrast: bool = False,
    ):
        """Guarda los parámetros de resize/compresión/contraste a aplicar por frame."""
        self.target_width = target_width
        self.jpeg_quality = jpeg_quality
        self.enhance_contrast = enhance_contrast

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Resize manteniendo aspecto (solo reduce, nunca agranda)."""
        h, w = frame.shape[:2]
        if w > self.target_width:
            scale = self.target_width / w
            frame = cv2.resize(
                frame,
                (self.target_width, int(h * scale)),
                interpolation=cv2.INTER_AREA,
            )
        if self.enhance_contrast:
            frame = self.enhance(frame)
        return frame

    @staticmethod
    def enhance(frame: np.ndarray) -> np.ndarray:
        """CLAHE sobre el canal de luminancia (mejora contraste sin alterar color)."""
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l_channel, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l_channel = clahe.apply(l_channel)
        return cv2.cvtColor(cv2.merge((l_channel, a, b)), cv2.COLOR_LAB2BGR)

    def to_base64(self, frame: np.ndarray) -> str:
        """Codifica el frame a JPEG y lo retorna como string base64 para la API de Claude."""
        ok, buf = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        )
        if not ok:
            raise RuntimeError("Fallo al codificar el frame a JPEG")
        return base64.standard_b64encode(buf.tobytes()).decode("utf-8")

    @staticmethod
    def frame_diff(prev: np.ndarray, curr: np.ndarray) -> float:
        """Diferencia media normalizada entre dos frames (0.0 a 1.0).

        Se compara en escala de grises y tamaño reducido: lo que importa es
        detectar cambio de escena, no el detalle.
        """
        small = (160, 90)
        g1 = cv2.cvtColor(cv2.resize(prev, small), cv2.COLOR_BGR2GRAY)
        g2 = cv2.cvtColor(cv2.resize(curr, small), cv2.COLOR_BGR2GRAY)
        return float(np.mean(cv2.absdiff(g1, g2)) / 255.0)
