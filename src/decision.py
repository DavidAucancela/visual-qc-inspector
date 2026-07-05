"""Motor de decisión: debounce y umbrales de confianza sobre los resultados."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from src.analyzer import InspectionResult


@dataclass
class FinalVerdict:
    status: str            # "PASS" | "WARN" | "FAIL"
    result: InspectionResult
    is_confirmed: bool     # True si pasó el debounce
    consecutive_count: int # cuántos seguidos del mismo estado


class DecisionEngine:
    def __init__(
        self,
        debounce_frames: int = 2,
        fail_threshold: float = 0.7,
        warn_threshold: float = 0.4,
    ):
        self.debounce_frames = max(1, debounce_frames)
        self.fail_threshold = fail_threshold
        self.warn_threshold = warn_threshold
        self._history: deque[str] = deque(maxlen=50)

    def reset(self) -> None:
        self._history.clear()

    def _adjust_by_confidence(self, result: InspectionResult) -> str:
        """Degrada el veredicto si la confianza no alcanza el umbral."""
        if result.verdict == "FAIL" and result.overall_confidence < self.fail_threshold:
            return "WARN"
        if result.verdict == "WARN" and result.overall_confidence < self.warn_threshold:
            return "PASS"
        return result.verdict

    def _consecutive(self, status: str) -> int:
        count = 0
        for s in reversed(self._history):
            if s != status:
                break
            count += 1
        return count

    def evaluate(self, result: InspectionResult) -> FinalVerdict:
        adjusted = self._adjust_by_confidence(result)
        self._history.append(adjusted)
        consecutive = self._consecutive(adjusted)

        # Defecto critical con confianza suficiente: FAIL inmediato, sin debounce
        has_critical = any(
            d.severity == "critical" and d.confidence >= self.fail_threshold
            for d in result.defects
        )
        if has_critical:
            return FinalVerdict("FAIL", result, True, consecutive)

        if adjusted == "FAIL":
            if consecutive >= self.debounce_frames:
                return FinalVerdict("FAIL", result, True, consecutive)
            # FAIL aislado (aún sin confirmar): se reporta como WARN
            return FinalVerdict("WARN", result, False, consecutive)

        return FinalVerdict(adjusted, result, True, consecutive)
