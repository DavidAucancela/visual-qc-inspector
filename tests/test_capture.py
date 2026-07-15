"""Tests de CameraCapture.try_reopen (reconexión) sin hardware real.

Se mockea cv2.VideoCapture con un doble que simula un dispositivo que abre y
entrega frame, uno que no abre, y uno que abre pero no captura.
"""

from __future__ import annotations

import numpy as np

import src.capture as capture_mod
from src.capture import CameraCapture


class _FakeCap:
    def __init__(self, opened=True, delivers=True):
        self._opened = opened
        self._delivers = delivers
        self.released = False

    def isOpened(self):
        return self._opened

    def set(self, *a):
        return True

    def read(self):
        if self._delivers:
            return True, np.zeros((4, 4, 3), dtype=np.uint8)
        return False, None

    def release(self):
        self.released = True


def _patch_videocapture(monkeypatch, fake):
    monkeypatch.setattr(capture_mod.cv2, "VideoCapture", lambda idx: fake)


def test_try_reopen_success(monkeypatch):
    fake = _FakeCap(opened=True, delivers=True)
    _patch_videocapture(monkeypatch, fake)
    cam = CameraCapture(device_id=0)
    assert cam.try_reopen() is True


def test_try_reopen_device_not_opened(monkeypatch):
    fake = _FakeCap(opened=False)
    _patch_videocapture(monkeypatch, fake)
    cam = CameraCapture(device_id=0)
    assert cam.try_reopen() is False


def test_try_reopen_opens_but_no_frame(monkeypatch):
    """Un dispositivo que abre pero no entrega frame cuenta como fallo."""
    fake = _FakeCap(opened=True, delivers=False)
    _patch_videocapture(monkeypatch, fake)
    cam = CameraCapture(device_id=0)
    assert cam.try_reopen() is False


def test_try_reopen_releases_previous_handle(monkeypatch):
    """try_reopen libera el handle previo antes de reabrir."""
    first = _FakeCap()
    cam = CameraCapture(device_id=0)
    cam._cap = first
    second = _FakeCap()
    _patch_videocapture(monkeypatch, second)
    cam.try_reopen()
    assert first.released is True
