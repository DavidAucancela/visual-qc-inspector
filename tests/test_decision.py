"""Tests del DecisionEngine: debounce y umbrales de confianza."""

from __future__ import annotations

from src.analyzer import Defect, InspectionResult
from src.decision import DecisionEngine


def make_result(verdict: str, confidence: float, defects: list[Defect] | None = None):
    return InspectionResult(
        verdict=verdict,
        overall_confidence=confidence,
        defects=defects or [],
        summary=f"mock {verdict}",
        evaluable=True,
        raw_response="{}",
    )


def make_engine(debounce_frames=3):
    return DecisionEngine(
        debounce_frames=debounce_frames, fail_threshold=0.7, warn_threshold=0.4
    )


def test_single_fail_becomes_warn_without_debounce():
    engine = make_engine(debounce_frames=3)
    verdict = engine.evaluate(make_result("FAIL", 0.9))

    assert verdict.status == "WARN"
    assert verdict.is_confirmed is False
    assert verdict.consecutive_count == 1


def test_three_consecutive_fails_confirm():
    engine = make_engine(debounce_frames=3)
    engine.evaluate(make_result("FAIL", 0.9))
    engine.evaluate(make_result("FAIL", 0.85))
    verdict = engine.evaluate(make_result("FAIL", 0.9))

    assert verdict.status == "FAIL"
    assert verdict.is_confirmed is True
    assert verdict.consecutive_count == 3


def test_pass_after_fails_resets_state():
    engine = make_engine(debounce_frames=3)
    engine.evaluate(make_result("FAIL", 0.9))
    engine.evaluate(make_result("FAIL", 0.9))
    engine.evaluate(make_result("PASS", 0.95))  # corta la racha
    verdict = engine.evaluate(make_result("FAIL", 0.9))

    assert verdict.status == "WARN"
    assert verdict.is_confirmed is False
    assert verdict.consecutive_count == 1


def test_fail_below_threshold_downgrades_to_warn():
    engine = make_engine(debounce_frames=1)
    # debounce=1 confirmaría el FAIL, pero la confianza 0.5 < 0.7 lo degrada
    verdict = engine.evaluate(make_result("FAIL", 0.5))

    assert verdict.status == "WARN"


def test_warn_below_threshold_downgrades_to_pass():
    engine = make_engine()
    verdict = engine.evaluate(make_result("WARN", 0.2))

    assert verdict.status == "PASS"


def test_critical_defect_fails_immediately_without_debounce():
    engine = make_engine(debounce_frames=3)
    critical = Defect(
        description="Componente quemado",
        severity="critical",
        location="centro",
        confidence=0.95,
    )
    verdict = engine.evaluate(make_result("FAIL", 0.9, defects=[critical]))

    assert verdict.status == "FAIL"
    assert verdict.is_confirmed is True


def test_pass_streak_counts_consecutive():
    engine = make_engine()
    engine.evaluate(make_result("PASS", 0.9))
    verdict = engine.evaluate(make_result("PASS", 0.9))

    assert verdict.status == "PASS"
    assert verdict.consecutive_count == 2


def test_major_defect_below_fail_on_severity_forces_fail_after_debounce():
    """fail_on_severity="minor" hace que un defecto "major" con confianza alta
    fuerce FAIL aunque el modelo haya devuelto WARN — sigue pasando por debounce."""
    engine = DecisionEngine(
        debounce_frames=2, fail_threshold=0.7, warn_threshold=0.4, fail_on_severity="minor"
    )
    major = Defect("Grieta", "major", "centro", 0.95)

    first = engine.evaluate(make_result("WARN", 0.9, defects=[major]))
    assert first.status == "WARN"
    assert first.is_confirmed is False

    second = engine.evaluate(make_result("WARN", 0.9, defects=[major]))
    assert second.status == "FAIL"
    assert second.is_confirmed is True


def test_major_defect_not_enforced_when_fail_on_severity_is_higher():
    """Con fail_on_severity="critical", un defecto "major" no debe forzar FAIL."""
    engine = DecisionEngine(
        debounce_frames=1, fail_threshold=0.7, warn_threshold=0.4, fail_on_severity="critical"
    )
    major = Defect("Grieta", "major", "centro", 0.95)

    verdict = engine.evaluate(make_result("WARN", 0.9, defects=[major]))
    assert verdict.status == "WARN"


def test_low_confidence_defect_does_not_force_fail():
    """Un defecto que alcanza la severidad pero con confianza baja no fuerza FAIL."""
    engine = DecisionEngine(
        debounce_frames=1, fail_threshold=0.7, warn_threshold=0.4, fail_on_severity="minor"
    )
    low_confidence_major = Defect("Grieta", "major", "centro", 0.5)

    verdict = engine.evaluate(make_result("WARN", 0.9, defects=[low_confidence_major]))
    assert verdict.status == "WARN"


def test_set_fail_on_severity_updates_threshold():
    engine = DecisionEngine(debounce_frames=1, fail_threshold=0.7, warn_threshold=0.4)
    major = Defect("Grieta", "major", "centro", 0.95)

    # Default fail_on_severity="major": el defecto ya alcanza el umbral -> FAIL directo
    assert engine.evaluate(make_result("WARN", 0.9, defects=[major])).status == "FAIL"

    engine.set_fail_on_severity("critical")
    verdict = engine.evaluate(make_result("WARN", 0.9, defects=[major]))
    assert verdict.status == "WARN"
