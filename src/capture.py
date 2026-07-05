"""Captura de video desde webcam con soporte de ROI."""

from __future__ import annotations

import cv2
import numpy as np

_ROI_WINDOW = "Seleccionar ROI (arrastrar y ENTER, C para cancelar)"


class CameraCapture:
    """Wrapper de cv2.VideoCapture con context manager y selección de ROI."""

    def __init__(self, device_id: int = 0, width: int = 1280, height: int = 720):
        self.device_id = device_id
        self.width = width
        self.height = height
        self._cap: cv2.VideoCapture | None = None

    def open(self) -> "CameraCapture":
        self._cap = cv2.VideoCapture(self.device_id)
        if not self._cap.isOpened():
            raise RuntimeError(
                f"No se pudo abrir la cámara con device_id={self.device_id}"
            )
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        return self

    def read(self) -> np.ndarray | None:
        """Lee un frame en BGR. None si la cámara no entrega imagen."""
        if self._cap is None:
            return None
        ok, frame = self._cap.read()
        return frame if ok else None

    def select_roi(self, frame: np.ndarray) -> tuple[int, int, int, int]:
        """Ventana interactiva: el usuario arrastra un rectángulo y presiona ENTER.

        Retorna (x, y, w, h). Si el usuario cancela, w y h son 0.
        """
        roi = cv2.selectROI(_ROI_WINDOW, frame, showCrosshair=True)
        cv2.destroyWindow(_ROI_WINDOW)
        return tuple(int(v) for v in roi)

    @staticmethod
    def crop_roi(frame: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
        x, y, w, h = roi
        if w <= 0 or h <= 0:
            return frame
        return frame[y : y + h, x : x + w]

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> "CameraCapture":
        return self.open()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
