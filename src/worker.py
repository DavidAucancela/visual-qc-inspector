"""Thread de análisis: ejecuta la llamada a Claude sin bloquear el loop de video.

Thread principal (30fps):           Thread de análisis (según throttle):
┌─────────────────────┐             ┌─────────────────────────┐
│ cam.read()          │  →frame→    │ analyzer.analyze()      │
│ dashboard.render()  │  ←result←   │ decision.evaluate()     │
│ cv2.imshow()        │             │ storage.save()          │
│ waitKey(1)          │             │ alerter.alert_fail()    │
└─────────────────────┘             └─────────────────────────┘
      ↑ estado compartido protegido por Lock
"""

from __future__ import annotations

import queue
import threading
import traceback

import anthropic
import numpy as np

from src.alerter import Alerter
from src.analyzer import VisionAnalyzer
from src.decision import DecisionEngine, FinalVerdict
from src.storage import Storage


class AnalysisWorker(threading.Thread):
    def __init__(
        self,
        analyzer: VisionAnalyzer,
        decision: DecisionEngine,
        storage: Storage,
        alerter: Alerter,
        session_id: int,
    ):
        """Conecta los componentes del pipeline de análisis (no arranca el thread todavía)."""
        super().__init__(daemon=True, name="analysis-worker")
        self.analyzer = analyzer
        self.decision = decision
        self.storage = storage
        self.alerter = alerter
        self.session_id = session_id

        # maxsize=1: si llega un frame mientras se analiza otro, se descarta —
        # no tiene sentido encolar frames viejos de un stream en vivo
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._latest: FinalVerdict | None = None
        self._is_analyzing = False
        self._last_error: str = ""
        self._stop_event = threading.Event()

    def submit(self, frame_b64: str, raw_frame: np.ndarray) -> bool:
        """Encola un frame para análisis. No bloquea; False si el worker está ocupado."""
        try:
            self._queue.put_nowait((frame_b64, raw_frame))
            return True
        except queue.Full:
            return False

    def get_latest(self) -> FinalVerdict | None:
        """Retorna el último veredicto disponible (para que el dashboard lo muestre)."""
        with self._lock:
            return self._latest

    @property
    def is_analyzing(self) -> bool:
        """True mientras hay una llamada a Claude en curso."""
        return self._is_analyzing

    @property
    def last_error(self) -> str:
        """Descripción corta del último error de análisis, o "" si el último intento fue exitoso."""
        with self._lock:
            return self._last_error

    def stop(self) -> None:
        """Señala al thread que termine su loop tras el ciclo actual."""
        self._stop_event.set()

    def run(self) -> None:
        """Loop del thread: toma frames de la cola, analiza, decide, persiste y alerta."""
        while not self._stop_event.is_set():
            try:
                frame_b64, raw_frame = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            self._is_analyzing = True
            try:
                result = self.analyzer.analyze(frame_b64)
                verdict = self.decision.evaluate(result)
                self.storage.save_inspection(
                    self.session_id, verdict, result, raw_frame
                )
                if verdict.status == "FAIL" and verdict.is_confirmed:
                    self.alerter.alert_fail(
                        result, (self.analyzer.profile or {}).get("name", "")
                    )
                elif verdict.status == "PASS":
                    self.alerter.alert_pass()
                with self._lock:
                    self._latest = verdict
                    self._last_error = ""
            except anthropic.APIError as exc:
                # Un error de API no tumba el loop: se registra y se sigue
                with self._lock:
                    self._last_error = f"API: {exc.__class__.__name__}"
            except Exception:
                traceback.print_exc()
                with self._lock:
                    self._last_error = "Error interno (ver consola)"
            finally:
                self._is_analyzing = False
