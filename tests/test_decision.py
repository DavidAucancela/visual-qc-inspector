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
