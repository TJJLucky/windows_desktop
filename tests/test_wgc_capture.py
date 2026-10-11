"""Python/native contract tests without driving the user's desktop."""
import ctypes
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from utils.core import screenshot as sc


class Native:
    def __init__(self):
        self.session = 10
        self.opens, self.closes, self.reads = [], [], []
        self.frame = 0
        self.status = 1
        self.stale = False
        self.size = (2, 2)
        self.active = self.max_active = 0

    def WgcClock100ns(self):
        return 1000

    def WgcOpen(self, hwnd, output):
        self.opens.append(hwnd)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64))[0] = self.session
        return 1

    def WgcClose(self, session):
        self.closes.append(session)
        return 1

    def WgcRead(self, session, buffer, capacity, barrier, timeout, output):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(.002)
        self.reads.append((session, barrier, timeout))
        self.frame += 1
        info = ctypes.cast(output, ctypes.POINTER(sc.WgcFrameInfo)).contents
        info.width, info.height = self.size
        info.required_bytes = self.size[0] * self.size[1] * 4
        info.generation, info.frame_number = self.session, self.frame
        info.frame_time_100ns = info.arrival_time_100ns = barrier if self.stale else barrier + 1
        status = self.status
        if capacity < info.required_bytes:
            status = -4
        if status == 1:
            ctypes.memset(buffer, self.frame, info.required_bytes)
        info.error = status
        self.active -= 1
        return status


@pytest.fixture
def capture(monkeypatch):
    native = Native()
    instance = object.__new__(sc.WGCCapture)
    instance._lock = threading.RLock()
    instance.WGCDLL = native
    instance._session, instance._hwnd = 0, None
    instance._buf_cache, instance._max_buf_len = None, 0
    monkeypatch.setattr(sc.WGCCapture, "_instance", instance)
    monkeypatch.setattr(sc.win32gui, "IsWindow", lambda h: True)
    monkeypatch.setattr(sc.win32gui, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(sc.win32gui, "IsIconic", lambda h: False)
    monkeypatch.setattr(sc.win32gui, "GetWindowRect", lambda h: (0, 0, 2, 2))
    yield instance, native
    sc.WGCCapture.release()


def test_session_reuse_images_own_their_pixels_and_equal_content_is_allowed(capture):
    instance, native = capture
    first = instance.capture_frame(1)
    pixels = first.image.tobytes()
    second = instance.capture_frame(1)
    assert first.info.generation == second.info.generation == 10
    assert second.info.frame_number > first.info.frame_number
    assert first.image.tobytes() == pixels != second.image.tobytes()
    native.frame -= 1
    third = instance.capture_frame(1)
    assert third.image.tobytes() == second.image.tobytes()
    assert native.closes == []


@pytest.mark.parametrize("status", [-3, -5, -6, -7, -8])
def test_errors_never_return_cached_image(capture, status):
    instance, native = capture
    instance.capture_frame(1)
    native.status = status
    with pytest.raises(sc.WgcCaptureError) as error:
        instance.capture_frame(1)
    assert error.value.status == status


def test_stale_native_success_is_rejected_and_action_barrier_is_forwarded(capture):
    instance, native = capture
    native.stale = True
    with pytest.raises(sc.WgcCaptureError, match="WGC_TIMEOUT"):
        instance.capture_frame(1, after_100ns=2000)
    assert native.reads[-1][1] == 2000


def test_resized_buffer_and_window_switch(capture):
    instance, native = capture
    native.size = (4, 4)
    assert instance.capture_frame(1).image.size == (4, 4)
    assert len(native.reads) == 2
    instance.capture_frame(2)
    assert native.closes == [10]


@pytest.mark.parametrize("check", ["IsWindow", "IsWindowVisible", "IsIconic"])
def test_unavailable_window_releases_session(capture, monkeypatch, check):
    instance, native = capture
    instance.capture_frame(1)
    monkeypatch.setattr(sc.win32gui, check, lambda h: check == "IsIconic")
    with pytest.raises(sc.WgcCaptureError, match="WINDOW_UNAVAILABLE"):
        instance.capture_frame(1)
    assert native.closes == [10]


def test_concurrent_reads_are_serialized_and_close_is_idempotent(capture):
    instance, native = capture
    with ThreadPoolExecutor(max_workers=4) as executor:
        frames = list(executor.map(lambda _: instance.capture_frame(1), range(8)))
    assert native.max_active == 1
    assert len({f.info.frame_number for f in frames}) == 8
    sc.WGCCapture.release()
    sc.WGCCapture.release()
    assert native.closes == [10]


def test_read_timeout_is_bounded(capture):
    instance, native = capture
    with pytest.raises(ValueError):
        instance.capture_frame(1, timeout_ms=301)
    assert native.reads == []
