"""区域定位结果缓存。

只缓存已识别区域的坐标和发送按钮坐标，不缓存截图或 OCR 内容。
窗口签名不变且在 TTL 内时复用；窗口变化或 TTL 到期后重新模板定位。
"""

from __future__ import annotations

import copy
import os
import threading
import time
from typing import Any


_DEFAULT_TTL_SECONDS = 30.0
_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def _ttl_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("QQ_REGION_CACHE_TTL", str(_DEFAULT_TTL_SECONDS))))
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _window_signature(window, image) -> tuple[Any, ...]:
    """返回窗口几何、状态和截图像素尺寸签名。"""
    return (
        int(getattr(window, "_hWnd", 0)),
        int(window.left),
        int(window.top),
        int(window.right),
        int(window.bottom),
        bool(getattr(window, "isMaximized", False)),
        int(image.width),
        int(image.height),
    )


def get_region(name: str, window, image) -> dict[str, Any] | None:
    """取得有效缓存，返回副本。"""
    key = (name, _window_signature(window, image))
    now = time.time()
    with _lock:
        item = _cache.get(key)
        if item is None:
            return None
        saved_at, value = item
        if now - saved_at >= _ttl_seconds():
            _cache.pop(key, None)
            return None
        return copy.deepcopy(value)


def save_region(name: str, window, image, value: dict[str, Any]) -> None:
    """保存区域坐标和可选发送按钮坐标。"""
    key = (name, _window_signature(window, image))
    with _lock:
        _cache[key] = (time.time(), copy.deepcopy(value))


def clear_region_cache() -> None:
    with _lock:
        _cache.clear()