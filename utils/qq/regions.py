"""QQ 窗口区域识别：好友列表、输入框、消息框的位置与截图。

依赖 core + vision + qq/window_ops。

思路：QQ 窗口布局相对固定，用模板图（templates/*.png）在整窗截图上定位关键锚点
（搜索栏左右角、输入框角、发送按钮、消息框角），再由锚点矩形推导各业务区域：
- 好友列表区：搜索栏下方到窗口底部；
- 输入框区：输入框左上角到发送按钮上方；
- 消息区：消息框左上角到输入框上方。

所有区域都同时给出 截图内坐标（image_region）与 屏幕坐标（screen_region），
前者用于裁剪图片做 OCR/模板匹配，后者用于鼠标点击。
"""

# os：拼接模板目录路径
import os
# Path：路径对象
from pathlib import Path

# PIL.Image：模板图加载与区域裁剪
from PIL import Image
# dataclass：RegionResult 数据类
from dataclasses import dataclass

# find_template：模板匹配定位锚点；crop_region：按区域裁剪；draw_box：调试画框
from ..vision.matcher import find_template, crop_region, draw_box
# timer：给区域识别函数自动打耗时日志
from ..core.timing import timer
# get_qq_window_image / get_qq_windows：窗口截图与窗口列表
from .window_ops import get_qq_window_image, get_qq_windows


@dataclass
class RegionResult:
    """一个业务区域的识别结果。

    - screen_region：屏幕绝对坐标（鼠标点击用）；
    - image_region：截图内相对坐标（裁剪/匹配用）；
    - image：按 image_region 裁出的区域图；
    - full_image：完整窗口截图（可选，调试/复用）；
    - send_button_region：发送按钮的屏幕坐标（输入框场景附带）。
    """

    # 屏幕绝对坐标（点击用）
    screen_region: dict
    # 截图内相对坐标（裁剪用）
    image_region: dict
    # 区域裁剪图
    image: Image.Image
    # 完整窗口截图（复用/调试）
    full_image: Image.Image | None = None
    # 发送按钮屏幕坐标（输入框场景）
    send_button_region: dict | None = None


# 模板目录：本文件在 utils/qq/，上溯三级到项目根再进 templates/
_TEMPLATE_DIR = os.path.join(Path(__file__).parent.parent.parent, "templates")


def load_image(path):
    """加载 templates 目录下的模板图。"""
    template_path = os.path.join(_TEMPLATE_DIR, path)
    return Image.open(template_path)


def _to_screen(window, region: dict) -> dict:
    """将图片相对坐标转为屏幕绝对坐标（用于鼠标点击）"""
    # 屏幕坐标 = 窗口屏幕左上角 + 截图内相对坐标
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
    """定位好友列表区域：搜索栏下方到窗口底部。返回 RegionResult。"""
    # 未传截图时现截一张（窗口需已就绪）
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    # 搜索栏左侧锚点模板
    left_search_bar_image = load_image("search_bar_left.png")

    # 只截窗口左上 20% 宽 × 10% 高的小图做搜索栏左锚点匹配（缩小搜索范围，提速）
    left_40_width = int(QQ_window_image.width * 0.2)
    left_40_height = int(QQ_window_image.height * 0.1)
    left_40_image = QQ_window_image.crop((0, 0, left_40_width, left_40_height))
    # 在小图上找搜索栏左侧图标（阈值 0.9 较严格）
    left_top_region = find_template(left_40_image, left_search_bar_image, 0.9)

    # 搜索栏右侧锚点模板：在整窗截图上找
    right_search_bar_image = load_image("search_bar_right.png")

    right_top_region = find_template(QQ_window_image, right_search_bar_image, 0.9)

    # 任一锚点缺失 → 界面布局对不上（版本变更/窗口异常），直接报错让上层兜底
    if left_top_region is None or right_top_region is None:
        raise RuntimeError(
            f"get_userList_region_and_image failed: left_top={left_top_region is not None} right_top={right_top_region is not None}")
    # 列表区域 = 搜索栏两角的最底部 → 窗口底边（列表顶边取两锚点 bottom 较大者）
    region = {"x": left_top_region["x"], "y": left_top_region["y"],
              "top": max(left_top_region['bottom'], right_top_region['bottom']),
              "bottom": QQ_window_image.height,
              "left": left_top_region['left'], "right": right_top_region['right']}
    # 返回屏幕坐标 + 截图坐标 + 区域图 + 整窗图
    return RegionResult(
        screen_region=_to_screen(window, region),
        image_region=region,
        image=crop_region(QQ_window_image, region),
        full_image=QQ_window_image,
    )


@timer
def get_input_buttom_region(window, QQ_window_image=None):
    """定位右下角发送按钮（屏幕坐标）；找不到返回 None。"""
    # 未传截图时现截
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    # 先找"未激活"态发送按钮图标
    input_buttom = load_image("input_buttom_deactive.png")
    right_bottom_region = find_template(QQ_window_image, input_buttom)

    if right_bottom_region is None:  # 那就看激活的图标
        # 未激活态找不到 → 试"激活"态图标（输入框有内容时按钮态会变）
        input_buttom = load_image("input_buttom_active.png")
        right_bottom_region = find_template(QQ_window_image, input_buttom)
    if right_bottom_region is None:
        # 两种态都找不到 → 无法定位发送按钮，返回 None 由调用方决定
        return None
    # 转成屏幕坐标返回
    return _to_screen(window, right_bottom_region)


@timer
def get_inputbox_region_and_image(window, QQ_window_image=None):
    """定位输入框区域：输入框左上角 → 发送按钮上方。返回 RegionResult（含发送按钮坐标）。"""
    # 未传截图时现截
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    # 左上角定位：输入框左上角图标（模板）
    inputbox_top_left = load_image("inputbox_top_left.png")
    left_top_region = find_template(QQ_window_image, inputbox_top_left, 0.9)
    # 右上角定位：输入框右上角图标（模板）
    inputbox_top_right = load_image("inputbox_top_right1.png")
    right_top_region = find_template(QQ_window_image, inputbox_top_right, 0.9)
    if right_top_region is None:  # 没找到,还有第二个图标
        # 第一套右上角模板找不到 → 试第二套（界面小差异）
        inputbox_top_right = load_image("inputbox_top_right2.png")
        right_top_region = find_template(QQ_window_image, inputbox_top_right, 0.9)

    # 底部锚点：发送按钮位置（输入框底边就在按钮上方）
    right_bottom_region = get_input_buttom_region(window, QQ_window_image)

    # 三个锚点任一缺失 → 布局对不上，报错
    if left_top_region is None or right_top_region is None or right_bottom_region is None:
        raise RuntimeError(
            f"get_inputbox_region_and_image failed: left_top={left_top_region is not None} right_top={right_top_region is not None} right_bottom={right_bottom_region is not None}")
    # 输入框区域：上边 = 两上角 bottom 较大者；底边 = 发送按钮 top 减 30（留出按钮与框的间隙）
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
        # 附带发送按钮屏幕坐标（点击发送时直接用，不必再匹配一次）
        send_button_region=right_bottom_region,
    )


@timer
def get_message_box_region_and_image(window, QQ_window_image=None):
    """定位消息框区域：消息框左上角 → 输入框上方。返回 RegionResult。"""
    # 未传截图时现截
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    # 左下角定位：复用输入框左上角图标模板（输入框与消息区共用同款角标）
    messagebox_bottom_left = load_image("inputbox_top_left.png")
    left_bottom_region = find_template(QQ_window_image, messagebox_bottom_left, 0.9)
    # 右下角定位：复用输入框右上角图标模板
    messagebox_bottom_right = load_image("inputbox_top_right1.png")
    right_bottom_region = find_template(QQ_window_image, messagebox_bottom_right, 0.9)
    if right_bottom_region is None:  # 没找到,还有第二个图标
        # 第二套右上角模板兜底
        messagebox_bottom_right = load_image("inputbox_top_right2.png")
        right_bottom_region = find_template(QQ_window_image, messagebox_bottom_right, 0.9)

    # 上部定位：消息框顶部栏图标（标题/时间区）
    messagebox_top = load_image("messagebox_top.png")
    top_region = find_template(QQ_window_image, messagebox_top, 0.9)

    # 三个锚点任一缺失 → 布局对不上，报错
    if left_bottom_region is None or right_bottom_region is None or top_region is None:
        raise RuntimeError(
            f"get_message_box_region_and_image failed: left_bottom={left_bottom_region is not None} right_bottom={right_bottom_region is not None} top={top_region is not None}")
    # 消息区：上边 = 顶栏 bottom；底边 = 两下角 top 较大者（消息区止于输入框开始处）
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
