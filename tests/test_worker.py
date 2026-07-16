"""Tests de AnalysisWorker: descarte de frames y resiliencia ante errores.

El loop de video nunca debe caerse por un fallo del análisis. Estos tests
verifican el descarte cuando hay un análisis en curso (queue maxsize=1) y que
un error de API o interno se registra sin tumbar el thread.
"""

from __future__ import annotations

import threading
import time

import anthropic
import httpx
import numpy as np

from src.analyzer import InspectionResult
from src.decision import FinalVerdict
from src.worker import AnalysisWorker


def _result(verdict="PASS"):
    return InspectionResult(
        verdict=verdict,
        overall_confidence=0.9,
        defects=[],
        summary="ok",
        evaluable=True,
        raw_response="{}",
    )


def _frame():
    return np.zeros((4, 4, 3), dtype=np.uint8)


def _wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


class FakeAnalyzer:
    def __init__(self, result=None, exc=None):
        self.result = result or _result()
        self.exc = exc
        self.calls = 0
        self.profile = {"name": "generic"}

    def analyze(self, frame_b64):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


class FakeDecision:
    def __init__(self, verdict):
        self.verdict = verdict

    def evaluate(self, result):
        return self.verdict


class FakeStorage:
    def __init__(self):
        self.saved = []
        self.saved_event = threading.Event()

    def save_inspection(self, session_id, verdict, result, frame):
        self.saved.append((session_id, verdict, result, frame))
        self.saved_event.set()


class FakeAlerter:
    def __init__(self):
        self.fail_calls = 0
        self.pass_calls = 0
        self.last_profile = None

    def alert_fail(self, result, profile_name=""):
        self.fail_calls += 1
        self.last_profile = profile_name

    def alert_pass(self):
        self.pass_calls += 1


def _make_worker(analyzer, verdict, storage=None, alerter=None):
    storage = storage or FakeStorage()
    alerter = alerter or FakeAlerter()
    worker = AnalysisWorker(
        analyzer, FakeDecision(verdict), storage, alerter, session_id=1
    )
    return worker, storage, alerter


def test_submit_drops_frame_when_busy():
    """Sin consumir la cola (thread sin arrancar), el segundo submit se descarta."""
    result = _result("PASS")
    worker, _, _ = _make_worker(FakeAnalyzer(result), FinalVerdict("PASS", result, True, 1))
    assert worker.submit("primero", _frame()) is True
    assert worker.submit("segundo", _frame()) is False  # cola llena -> descartado


def test_pass_verdict_triggers_pass_alert_and_latest():
    result = _result("PASS")
    verdict = FinalVerdict("PASS", result, True, 1)
    worker, storage, alerter = _make_worker(FakeAnalyzer(result), verdict)
    worker.start()
    try:
        assert worker.submit("b64", _frame()) is True
        assert storage.saved_event.wait(timeout=2)
        assert worker.get_latest() is verdict
        assert alerter.pass_calls == 1
        assert alerter.fail_calls == 0
        assert worker.last_error == ""
    finally:
        worker.stop()
        worker.join(timeout=2)


def test_confirmed_fail_triggers_fail_alert():
    result = _result("FAIL")
    verdict = FinalVerdict("FAIL", result, True, 2)
    worker, storage, alerter = _make_worker(FakeAnalyzer(result), verdict)
    worker.start()
    try:
        worker.submit("b64", _frame())
        assert storage.saved_event.wait(timeout=2)
        assert _wait_until(lambda: alerter.fail_calls == 1)
        assert alerter.pass_calls == 0
    finally:
        worker.stop()
        worker.join(timeout=2)


def test_unconfirmed_fail_does_not_alert():
    """Un FAIL sin confirmar (debounce) no debe disparar alerta sonora."""
    result = _result("FAIL")
    verdict = FinalVerdict("FAIL", result, is_confirmed=False, consecutive_count=1)
    worker, storage, alerter = _make_worker(FakeAnalyzer(result), verdict)
    worker.start()
    try:
        worker.submit("b64", _frame())
        assert storage.saved_event.wait(timeout=2)
        assert alerter.fail_calls == 0
        assert alerter.pass_calls == 0
    finally:
        worker.stop()
        worker.join(timeout=2)


def test_api_error_is_recorded_without_killing_loop():
    result = _result("PASS")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    analyzer = FakeAnalyzer(exc=anthropic.APIConnectionError(message="down", request=req))
    worker, _, _ = _make_worker(analyzer, FinalVerdict("PASS", result, True, 1))
    worker.start()
    try:
        worker.submit("b64", _frame())
        assert _wait_until(lambda: worker.last_error != "")
        assert "APIConnectionError" in worker.last_error
        assert worker.is_alive()  # el loop sobrevive al error de API
    finally:
        worker.stop()
        worker.join(timeout=2)


def test_internal_error_is_recorded_without_killing_loop():
    result = _result("PASS")
    analyzer = FakeAnalyzer(exc=RuntimeError("boom"))
    worker, _, _ = _make_worker(analyzer, FinalVerdict("PASS", result, True, 1))
    worker.start()
    try:
        worker.submit("b64", _frame())
        assert _wait_until(lambda: worker.last_error != "")
        assert "interno" in worker.last_error.lower()
        assert worker.is_alive()
    finally:
        worker.stop()
        worker.join(timeout=2)
