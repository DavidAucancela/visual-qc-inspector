"""Motor de decisión: debounce y umbrales de confianza sobre los resultados."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from src.analyzer import InspectionResult

# Orden de severidad, de menor a mayor impacto
SEVERITY_ORDER = ["cosmetic", "minor", "major", "critical"]


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
        fail_on_severity: str = "major",
    ):
        """Guarda los umbrales de confianza y el tamaño de la ventana de debounce."""
        self.debounce_frames = max(1, debounce_frames)
        self.fail_threshold = fail_threshold
        self.warn_threshold = warn_threshold
        self.fail_on_severity = fail_on_severity
        self._history: deque[str] = deque(maxlen=50)

    def reset(self) -> None:
        """Limpia el historial de veredictos (se usa al cambiar de perfil)."""
        self._history.clear()

    def set_fail_on_severity(self, severity: str) -> None:
        """Actualiza el umbral de severidad que dispara FAIL (al cambiar de perfil)."""
        self.fail_on_severity = severity

    def _meets_fail_severity(self, severity: str) -> bool:
        """True si `severity` alcanza o supera `fail_on_severity` en SEVERITY_ORDER."""
        try:
            return SEVERITY_ORDER.index(severity) >= SEVERITY_ORDER.index(self.fail_on_severity)
        except ValueError:
            return False

    def _adjust_by_confidence(self, result: InspectionResult) -> str:
        """Degrada el veredicto si la confianza no alcanza el umbral."""
        if result.verdict == "FAIL" and result.overall_confidence < self.fail_threshold:
            return "WARN"
        if result.verdict == "WARN" and result.overall_confidence < self.warn_threshold:
            return "PASS"
        return result.verdict

    def _consecutive(self, status: str) -> int:
        """Cuenta cuántas veces seguidas (desde el final del historial) se repite `status`."""
        count = 0
        for s in reversed(self._history):
            if s != status:
                break
            count += 1
        return count

    def evaluate(self, result: InspectionResult) -> FinalVerdict:
        """Aplica ajuste por confianza y debounce sobre un resultado crudo de Claude y produce el veredicto final."""
        adjusted = self._adjust_by_confidence(result)

        # El veredicto crudo del modelo puede subestimar la severidad global
        # (más frecuente con modelos económicos como Haiku). Si algún defecto
        # alcanza el umbral fail_on_severity del perfil con confianza
        # suficiente, la política del perfil manda sobre ese veredicto crudo.
        meets_fail_policy = any(
            self._meets_fail_severity(d.severity) and d.confidence >= self.fail_threshold
            for d in result.defects
        )
        if meets_fail_policy and adjusted != "FAIL":
            adjusted = "FAIL"

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
