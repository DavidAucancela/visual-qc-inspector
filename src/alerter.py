"""Alertas: sonido, notificación del OS y webhook. Todas no-bloqueantes."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import threading

import requests

from src.analyzer import InspectionResult

_IS_MAC = platform.system() == "Darwin"
_IS_WIN = platform.system() == "Windows"

FAIL_SOUND_MAC = "/System/Library/Sounds/Basso.aiff"
PASS_SOUND_MAC = "/System/Library/Sounds/Glass.aiff"


class Alerter:
    def __init__(self, settings: dict, webhook_url: str = ""):
        """Lee la sección `alerts` de settings.yaml para saber qué alertas están activas."""
        alerts = settings.get("alerts", {})
        self.sound_on_fail = alerts.get("sound_on_fail", True)
        self.sound_on_pass = alerts.get("sound_on_pass", False)
        self.webhook_on_fail = alerts.get("webhook_on_fail", False)
        self.os_notification = alerts.get("os_notification", True)
        self.webhook_url = webhook_url

    def alert_fail(self, result: InspectionResult, profile_name: str = "") -> None:
        """Dispara sonido, notificación del OS y/o webhook para un FAIL confirmado."""
        if self.sound_on_fail:
            self._run_async(self._play_sound, FAIL_SOUND_MAC)
        if self.os_notification:
            self._run_async(self._notify, "DEFECTO DETECTADO", result.summary)
        if self.webhook_on_fail and self.webhook_url:
            self._run_async(self._send_webhook, result, profile_name)

    def alert_pass(self) -> None:
        """Reproduce sonido de PASS si `sound_on_pass` está activado en config."""
        if self.sound_on_pass:
            self._run_async(self._play_sound, PASS_SOUND_MAC)

    @staticmethod
    def _run_async(fn, *args) -> None:
        """Ejecuta `fn` en un thread daemon para no bloquear el worker de análisis."""
        threading.Thread(target=fn, args=args, daemon=True).start()

    @staticmethod
    def _play_sound(mac_sound_path: str) -> None:
        """Reproduce un sonido según el OS (afplay en mac, winsound en Windows, campana ASCII de fallback)."""
        try:
            if _IS_MAC:
                subprocess.run(["afplay", mac_sound_path], check=False, timeout=5)
            elif _IS_WIN:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONHAND)
            else:
                # Fallback portable: campana de terminal
                sys.stdout.write("\a")
                sys.stdout.flush()
        except Exception:
            pass  # una alerta fallida nunca debe afectar la inspección

    @staticmethod
    def _notify(title: str, message: str) -> None:
        """Muestra una notificación nativa del OS (solo implementado en mac vía osascript)."""
        try:
            if _IS_MAC:
                script = f'display notification "{message}" with title "{title}"'
                subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        except Exception:
            pass

    def _send_webhook(self, result: InspectionResult, profile_name: str = "") -> None:
        """POST JSON compatible con webhooks de Slack/Discord ("text"/"content").

        Payload enriquecido: perfil activo + cada defecto con su severidad y
        confianza en una línea, para que la alerta sea accionable sin abrir la app.
        """
        if result.defects:
            defects = "\n".join(
                f"  - [{d.severity}] {d.description}"
                f" — {d.location} ({d.confidence:.0%})"
                for d in result.defects
            )
        else:
            defects = "  - sin detalle"
        header = ":rotating_light: QC FAIL"
        if profile_name:
            header += f" [{profile_name}]"
        text = (
            f"{header} — {result.summary}\n"
            f"Confianza global: {result.overall_confidence:.0%}\n"
            f"Defectos ({len(result.defects)}):\n{defects}"
        )
        payload = {"text": text, "content": text}
        try:
            requests.post(
                self.webhook_url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
        except requests.RequestException:
            pass
