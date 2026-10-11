"""区域定位缓存测试。"""

from __future__ import annotations

from PIL import Image
import pytest

from utils.qq import region_cache
from utils.qq.region_cache import clear_region_cache, get_region, save_region
from utils.qq import template_runtime


class _Window:
    _hWnd = 123
    left = 10
    top = 20
    right = 810
    bottom = 620
    isMaximized = False


@pytest.fixture(autouse=True)
def cache_environment(monkeypatch):
    clear_region_cache()
    monkeypatch.delenv("QQ_REGION_CACHE_TTL", raising=False)
    state = dict(pid=456, dpi=96)
    monkeypatch.setattr(template_runtime, "window_signature", lambda window: dict(
        hwnd=window._hWnd, left=window.left, top=window.top, right=window.right, bottom=window.bottom, **state))
    yield state
    clear_region_cache()


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
    monkeypatch.setenv("QQ_REGION_CACHE_TTL", "60")
    monkeypatch.setattr(region_cache.time, "monotonic", lambda: 100)
    save_region("inputbox", window, image, {"region": {"left": 1}})
    monkeypatch.setattr(region_cache.time, "monotonic", lambda: 159.9)
    assert get_region("inputbox", window, image) is not None
    monkeypatch.setattr(region_cache.time, "monotonic", lambda: 160)
    assert get_region("inputbox", window, image) is None


def test_default_cache_remains_valid_after_a_day(monkeypatch):
    window, image = _Window(), Image.new("RGB", (800, 600))
    monkeypatch.setattr(region_cache.time, "monotonic", lambda: 100)
    save_region("userlist", window, image, {"region": {"left": 1}})
    monkeypatch.setattr(region_cache.time, "monotonic", lambda: 100 + 86400)
    assert get_region("userlist", window, image) == {"region": {"left": 1}}


@pytest.mark.parametrize("attribute", ["left", "top", "right", "bottom", "_hWnd", "isMaximized"])
def test_changed_window_invalidates_cache_even_after_returning(attribute):
    window, image = _Window(), Image.new("RGB", (800, 600))
    save_region("userlist", window, image, {"region": {"left": 1}})
    original = getattr(window, attribute)
    setattr(window, attribute, not original if isinstance(original, bool) else original + 1)
    assert get_region("userlist", window, image) is None
    setattr(window, attribute, original)
    assert get_region("userlist", window, image) is None


@pytest.mark.parametrize("attribute", ["pid", "dpi"])
def test_same_geometry_with_changed_process_or_dpi_invalidates(cache_environment, attribute):
    window, image = _Window(), Image.new("RGB", (800, 600))
    save_region("userlist", window, image, {"region": {"left": 1}})
    cache_environment[attribute] += 1
    assert get_region("userlist", window, image) is None


def test_different_template_store_invalidates_cache(monkeypatch):
    from types import SimpleNamespace
    window, image = _Window(), Image.new("RGB", (800, 600))
    monkeypatch.setattr(template_runtime, "_store", SimpleNamespace(boot_id="first", active_id=lambda: "same-profile"))
    save_region("userlist", window, image, {"region": {"left": 1}})
    monkeypatch.setattr(template_runtime, "_store", SimpleNamespace(boot_id="second", active_id=lambda: "same-profile"))
    assert get_region("userlist", window, image) is None


def test_changed_content_keeps_cache_but_changed_anchor_invalidates():
    window, image = _Window(), Image.new("RGB", (800, 600), "white")
    value = dict(region={"left": 1}, templates={"search_bar_right": dict(
        status="matched", imageRect=dict(left=250, top=20, right=300, bottom=50))})
    save_region("userlist", window, image, value)
    # 联系人/消息更新不能导致固定区域失效。
    updated = image.copy()
    updated.paste("black", (50, 100, 250, 550))
    assert get_region("userlist", window, updated) == value
    # 同尺寸窗口也可能调整列表宽度；旧锚点消失时必须重新定位。
    updated.paste("black", (250, 20, 300, 50))
    assert get_region("userlist", window, updated) is None
