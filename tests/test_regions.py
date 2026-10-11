"""使用测试机真实截图调用生产区域定位代码。"""

from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from utils.qq.region_cache import clear_region_cache
from utils.qq import template_runtime
from utils.qq.regions import (
    get_inputbox_region_and_image, get_message_box_region_and_image,
    get_userList_region_and_image,
)


@pytest.fixture(autouse=True)
def fake_window_signature(monkeypatch):
    monkeypatch.setattr(template_runtime, "window_signature", lambda window: dict(
        hwnd=window._hWnd, pid=88, dpi=96, left=window.left, top=window.top,
        right=window.right, bottom=window.bottom))


def _debug_image(name: str) -> Image.Image:
    path = Path(__file__).parents[1] / "debug" / name
    assert path.exists(), f"缺少测试机真实界面截图: {path}"
    return Image.open(path).convert("RGB")


def _window(image: Image.Image):
    return SimpleNamespace(
        _hWnd=hash(image.tobytes()[:32]), left=0, top=0, right=image.width, bottom=image.height,
        isMaximized=False,
    )


def _locate_all(name: str):
    clear_region_cache()
    image = _debug_image(name)
    window = _window(image)
    return (
        get_userList_region_and_image(window, image).image_region,
        get_message_box_region_and_image(window, image).image_region,
        get_inputbox_region_and_image(window, image).image_region,
    )


def test_group_chat_real_machine_regions():
    user_list, message, input_box = _locate_all("395bc17091b5d2d664d3795adb84bebb.png")

    assert user_list == {
        "x": 60, "y": 56, "top": 56, "bottom": 1032, "left": 60, "right": 313,
    }
    assert message == {
        "x": 307, "y": 15, "top": 70, "bottom": 815, "left": 307, "right": 783,
    }
    assert input_box == {
        "x": 307, "y": 871, "top": 871, "bottom": 962, "left": 307, "right": 783,
    }


def test_private_chat_real_machine_regions():
    user_list, message, input_box = _locate_all("655d3acd056c0ef6e31363698cda717e.png")

    assert user_list == {
        "x": 60, "y": 56, "top": 56, "bottom": 1033, "left": 60, "right": 314,
    }
    assert message == {
        "x": 308, "y": 17, "top": 68, "bottom": 815, "left": 308, "right": 957,
    }
    assert input_box == {
        "x": 308, "y": 871, "top": 871, "bottom": 963, "left": 308, "right": 957,
    }


def test_input_region_and_enter_send_do_not_require_button(monkeypatch):
    from utils.qq import regions, input as qq_input
    clear_region_cache()
    original_load = regions.load_image
    def load_without_button(filename):
        assert not filename.startswith("input_buttom")
        return original_load(filename)
    monkeypatch.setattr(regions, "load_image", load_without_button)
    image = _debug_image("655d3acd056c0ef6e31363698cda717e.png")
    result = regions.get_inputbox_region_and_image(_window(image), image)
    assert result.send_button_region is None
    assert result.image_region["bottom"] == image.height - 70
    calls = []
    monkeypatch.setattr(qq_input, "send_hotkey", lambda keys: calls.append(keys))
    monkeypatch.setattr(qq_input.time, "sleep", lambda seconds: None)
    qq_input.InputBox.send_message(SimpleNamespace(input_region=result))
    assert calls == [("enter",)]
