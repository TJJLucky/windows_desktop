"""窗口状态 SQLite 缓存测试。"""

from __future__ import annotations

import time

from utils.qq.window_cache import WindowSnapshot, WindowStateCache


def _snapshot(now: float) -> WindowSnapshot:
    return WindowSnapshot(
        hwnd=123,
        pid=456,
        title="QQ",
        rect=(0, 0, 800, 600),
        minimized=False,
        maximized=False,
        topmost=True,
        dpi=144,
        last_checked_at=now,
        last_full_refresh_at=now,
    )


def test_window_cache_roundtrip_and_match(tmp_path):
    cache = WindowStateCache(tmp_path / "window.sqlite3")
    now = time.time()
    snapshot = _snapshot(now)
    cache.save(snapshot)

    loaded = cache.get(snapshot.hwnd)
    assert loaded == snapshot
    assert loaded is not None
    assert loaded.matches(
        pid=456,
        title="QQ",
        rect=(0, 0, 800, 600),
        minimized=False,
        maximized=False,
        topmost=True,
        dpi=144,
    )
    assert not loaded.matches(
        pid=456,
        title="QQ",
        rect=(1, 0, 800, 600),
        minimized=False,
        maximized=False,
        topmost=True,
        dpi=144,
    )


def test_window_cache_touch_and_full_refresh_due(tmp_path):
    cache = WindowStateCache(tmp_path / "window.sqlite3")
    now = time.time()
    snapshot = _snapshot(now)
    cache.save(snapshot)
    cache.touch(snapshot.hwnd, checked_at=now + 5)

    loaded = cache.get(snapshot.hwnd)
    assert loaded is not None
    assert loaded.last_checked_at == now + 5
    assert not loaded.full_refresh_due(now + 29, 30)
    assert loaded.full_refresh_due(now + 30, 30)