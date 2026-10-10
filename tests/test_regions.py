"""使用测试机真实截图调用生产区域定位代码。"""

from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from utils.qq.region_cache import clear_region_cache
from utils.qq.regions import (
    get_inputbox_region_and_image, get_message_box_region_and_image,
    get_userList_region_and_image,
)


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
