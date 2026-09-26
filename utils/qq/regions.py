"""QQ 窗口区域识别：好友列表、输入框、消息框的位置与截图。

依赖 core + vision + qq/composites。
"""

import sys
import os
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image
from dataclasses import dataclass

try:
    from ..vision.matcher import find_template, crop_region, draw_box
    from ..core.windows import get_qq_windows, timer
    from .composites import ensure_qq_window_with_retry, get_qq_window_image
except ImportError:
    from utils.vision.matcher import find_template, crop_region, draw_box
    from utils.core.windows import get_qq_windows, timer
    from utils.qq.composites import ensure_qq_window_with_retry, get_qq_window_image


@dataclass
class RegionResult:
    screen_region: dict
    image_region: dict
    image: Image.Image
    full_image: Image.Image | None = None
    send_button_region: dict | None = None


_TEMPLATE_DIR = os.path.join(Path(__file__).parent.parent.parent, "templates")


def load_image(path):
    template_path = os.path.join(_TEMPLATE_DIR, path)
    return Image.open(template_path)


def _to_screen(window, region: dict) -> dict:
    """将图片相对坐标转为屏幕绝对坐标（用于鼠标点击）"""
    left = int(window.left + region["left"])
    top = int(window.top + region["top"])
    right = int(window.left + region["right"])
    bottom = int(window.top + region["bottom"])
    return {
        "x": left,
        "y": top,
        "w": right - left,
        "h": bottom - top,
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
    }


@timer
def get_userList_region_and_image(window, QQ_window_image=None):
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    left_search_bar_image = load_image("search_bar_left.png")

    left_40_width = int(QQ_window_image.width * 0.2)
    left_40_height = int(QQ_window_image.height * 0.1)
    left_40_image = QQ_window_image.crop((0, 0, left_40_width, left_40_height))
    # left_40_image.show()
    left_top_region = find_template(left_40_image, left_search_bar_image, 0.9)

    right_search_bar_image = load_image("search_bar_right.png")

    right_top_region = find_template(QQ_window_image, right_search_bar_image, 0.9)

    if left_top_region is None or right_top_region is None:
        raise RuntimeError(
            f"get_userList_region_and_image failed: left_top={left_top_region is not None} right_top={right_top_region is not None}")
    region = {"x": left_top_region["x"], "y": left_top_region["y"],
              "top": max(left_top_region['bottom'], right_top_region['bottom']),
              "bottom": QQ_window_image.height,
              "left": left_top_region['left'], "right": right_top_region['right']}
    return RegionResult(
        screen_region=_to_screen(window, region),
        image_region=region,
        image=crop_region(QQ_window_image, region),
        full_image=QQ_window_image,
    )


@timer
def get_input_buttom_region(window, QQ_window_image=None):
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    # 右下角发送按钮定位,没激活的图标
    input_buttom = load_image("input_buttom_deactive.png")
    right_bottom_region = find_template(QQ_window_image, input_buttom)

    if right_bottom_region is None:  # 那就看激活的图标
        input_buttom = load_image("input_buttom_active.png")
        right_bottom_region = find_template(QQ_window_image, input_buttom)
    if right_bottom_region is None:
        return None
    return _to_screen(window, right_bottom_region)


@timer
def get_inputbox_region_and_image(window, QQ_window_image=None):
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    # 左上角定位
    inputbox_top_left = load_image("inputbox_top_left.png")
    left_top_region = find_template(QQ_window_image, inputbox_top_left, 0.9)
    # 右上角定位
    inputbox_top_right = load_image("inputbox_top_right1.png")
    right_top_region = find_template(QQ_window_image, inputbox_top_right, 0.9)
    if right_top_region is None:  # 没找到,还有第二个图标
        inputbox_top_right = load_image("inputbox_top_right2.png")
        right_top_region = find_template(QQ_window_image, inputbox_top_right, 0.9)

    right_bottom_region = get_input_buttom_region(window, QQ_window_image)

    if left_top_region is None or right_top_region is None or right_bottom_region is None:
        raise RuntimeError(
            f"get_inputbox_region_and_image failed: left_top={left_top_region is not None} right_top={right_top_region is not None} right_bottom={right_bottom_region is not None}")
    region = {"x": left_top_region["x"], "y": left_top_region["y"],
              "top": max(left_top_region['bottom'], right_top_region['bottom']),
              "bottom": right_bottom_region["top"] - window.top - 30,
              # right_bottom_region 是屏幕坐标，转回图片坐标（减 window.top）再取输入框底部
              "left": left_top_region['left'], "right": right_top_region['right']}
    return RegionResult(
        screen_region=_to_screen(window, region),
        image_region=region,
        image=crop_region(QQ_window_image, region),
        full_image=QQ_window_image,
        send_button_region=right_bottom_region,
    )


@timer
def get_message_box_region_and_image(window, QQ_window_image=None):
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    # 左下角定位
    messagebox_bottom_left = load_image("inputbox_top_left.png")
    left_bottom_region = find_template(QQ_window_image, messagebox_bottom_left, 0.9)
    # 右下角定位
    messagebox_bottom_right = load_image("inputbox_top_right1.png")
    right_bottom_region = find_template(QQ_window_image, messagebox_bottom_right, 0.9)
    if right_bottom_region is None:  # 没找到,还有第二个图标
        messagebox_bottom_right = load_image("inputbox_top_right2.png")
        right_bottom_region = find_template(QQ_window_image, messagebox_bottom_right, 0.9)

    # 上部定位
    messagebox_top = load_image("messagebox_top.png")
    top_region = find_template(QQ_window_image, messagebox_top, 0.9)

    if left_bottom_region is None or right_bottom_region is None or top_region is None:
        raise RuntimeError(
            f"get_message_box_region_and_image failed: left_bottom={left_bottom_region is not None} right_bottom={right_bottom_region is not None} top={top_region is not None}")
    region = {"x": left_bottom_region["x"], "y": top_region["y"],
              "top": top_region['bottom'],
              "bottom": max(left_bottom_region['top'], right_bottom_region['top']),
              "left": left_bottom_region['left'], "right": right_bottom_region['right']}
    return RegionResult(
        screen_region=_to_screen(window, region),
        image_region=region,
        image=crop_region(QQ_window_image, region),
        full_image=QQ_window_image,
    )


if __name__ == "__main__":
    window = ensure_qq_window_with_retry()
    if window is None:
        raise RuntimeError("QQ 窗口未就绪")
    QQ_window_image, _ = get_qq_window_image(window)
    if QQ_window_image is None:
        raise RuntimeError("QQ 窗口截图失败")
    print(get_input_buttom_region(window, QQ_window_image))
    userlist_result = get_userList_region_and_image(window, QQ_window_image)
    userlist_result.image.show()
    # inputbox_result = get_inputbox_region_and_image(window, QQ_window_image)
    # inputbox_result.image.show()
    # msg_result = get_message_box_region_and_image(window, QQ_window_image)
    # msg_result.image.show()
