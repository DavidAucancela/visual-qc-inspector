"""Decide qué frames se envían a análisis (throttling de llamadas a la API)."""

from __future__ import annotations

import time

import numpy as np

from src.preprocessor import Preprocessor


class FrameSelector:
    """Modos:

    - "timer":  analiza cada `timer_interval_sec` segundos.
    - "diff":   analiza cuando el frame cambia más que `diff_threshold`.
    - "manual": analiza solo cuando se llamó a trigger() (tecla SPACE).
    """

    VALID_MODES = ("timer", "diff", "manual")

    def __init__(
        self,
        mode: str = "timer",
        timer_interval_sec: float = 3.0,
        diff_threshold: float = 0.15,
    ):
        if mode not in self.VALID_MODES:
            raise ValueError(f"Modo inválido: {mode!r} (usar {self.VALID_MODES})")
        self.mode = mode
        self.timer_interval_sec = timer_interval_sec
        self.diff_threshold = diff_threshold
        self._last_analysis_ts: float = 0.0
        self._prev_frame: np.ndarray | None = None
        self._triggered = False

    def trigger(self) -> None:
        """Fuerza que el próximo should_analyze retorne True (en cualquier modo)."""
        self._triggered = True

    def should_analyze(self, frame: np.ndarray) -> bool:
        if self._triggered:
            self._triggered = False
            self._mark_analyzed(frame)
            return True

        if self.mode == "timer":
            if time.monotonic() - self._last_analysis_ts >= self.timer_interval_sec:
                self._mark_analyzed(frame)
                return True
            return False

        if self.mode == "diff":
            if self._prev_frame is None:
                # Primer frame: analizarlo establece la línea base
                self._mark_analyzed(frame)
                return True
            diff = Preprocessor.frame_diff(self._prev_frame, frame)
            if diff > self.diff_threshold:
                self._mark_analyzed(frame)
                return True
            return False

        # manual: solo trigger()
        return False

    def _mark_analyzed(self, frame: np.ndarray) -> None:
        self._last_analysis_ts = time.monotonic()
        self._prev_frame = frame.copy()
