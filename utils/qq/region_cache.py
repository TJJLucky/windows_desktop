"""区域定位结果缓存。

只缓存已识别区域的坐标和模板定位信息，不缓存截图或 OCR 内容。
默认按窗口和模板版本变化失效；可显式配置 TTL 强制定期重新定位。
"""

from __future__ import annotations

import copy
import hashlib
import os
import threading
import time
from typing import Any


_DEFAULT_TTL_SECONDS = 0.0
_cache: dict[str, tuple[tuple[Any, ...], float, dict[str, Any], tuple]] = {}
_lock = threading.Lock()


def _ttl_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("QQ_REGION_CACHE_TTL", str(_DEFAULT_TTL_SECONDS))))
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _window_signature(window, image) -> tuple[Any, ...]:
    """返回版本、进程、DPI、窗口几何和截图像素尺寸签名。"""
    from .template_runtime import profile_cache_key, window_signature
    return (
        profile_cache_key(),
        tuple(sorted(window_signature(window).items())),
        bool(getattr(window, "isMaximized", False)),
        int(image.width),
        int(image.height),
    )


def _anchor_fingerprint(image, value) -> tuple:
    """只保存锚点像素摘要，识别同尺寸窗口内的布局变化；无需重新匹配。"""
    result = []
    for key, item in sorted(value.get("templates", {}).items()):
        if item.get("status") != "matched" or key not in {
            "search_bar_left", "search_bar_right", "inputbox_top_left",
            "inputbox_top_right1", "inputbox_top_right2", "messagebox_top",
        }:
            continue
        rect = item["imageRect"]
        box = tuple(rect[side] for side in ("left", "top", "right", "bottom"))
        result.append((key, box, hashlib.sha256(image.crop(box).tobytes()).digest()))
    return tuple(result)


def get_region(name: str, window, image) -> dict[str, Any] | None:
    """取得有效缓存，返回副本。"""
    signature = _window_signature(window, image)
    now = time.monotonic()
    with _lock:
        item = _cache.get(name)
        if item is None:
            return None
        saved_signature, saved_at, value, anchors = item
        ttl = _ttl_seconds()
        if (signature != saved_signature or (ttl > 0 and now - saved_at >= ttl) or
                _anchor_fingerprint(image, value) != anchors):
            _cache.pop(name, None)
            return None
        return copy.deepcopy(value)


def save_region(name: str, window, image, value: dict[str, Any]) -> None:
    """每个区域只保留最新窗口状态，避免移回旧位置后复活旧缓存。"""
    signature = _window_signature(window, image)
    with _lock:
        _cache[name] = (signature, time.monotonic(), copy.deepcopy(value), _anchor_fingerprint(image, value))


def clear_region_cache() -> None:
    with _lock:
        _cache.clear()
