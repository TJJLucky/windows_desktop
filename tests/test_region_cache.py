"""区域定位缓存测试。"""

from __future__ import annotations

from PIL import Image

from utils.qq import region_cache
from utils.qq.region_cache import clear_region_cache, get_region, save_region


class _Window:
    _hWnd = 123
    left = 10
    top = 20
    right = 810
    bottom = 620
    isMaximized = False


def test_region_cache_roundtrip_and_signature_change():
    clear_region_cache()
    window = _Window()
    image = Image.new("RGB", (800, 600))
    region = {"left": 1, "top": 2, "right": 300, "bottom": 500}

    save_region("userlist", window, image, {"region": region, "send_button_region": None})
    loaded = get_region("userlist", window, image)
    assert loaded == {"region": region, "send_button_region": None}

    # 调用方修改返回值不能污染缓存。
    loaded["region"]["left"] = 99
    assert get_region("userlist", window, image)["region"]["left"] == 1

    # 窗口截图尺寸变化后缓存失效。
    changed_image = Image.new("RGB", (801, 600))
    assert get_region("userlist", window, changed_image) is None


def test_region_cache_expires(monkeypatch):
    clear_region_cache()
    window = _Window()
    image = Image.new("RGB", (800, 600))
    save_region("inputbox", window, image, {"region": {"left": 1}})

    monkeypatch.setattr(region_cache, "_ttl_seconds", lambda: 0)
    assert get_region("inputbox", window, image) is None