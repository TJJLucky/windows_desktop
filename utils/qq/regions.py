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
# wraps：保留被缓存函数的元信息
from functools import wraps
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
# 区域定位结果短期缓存
from .region_cache import get_region, save_region


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

# QQ 进入多消息选择态后，操作工具栏位于窗口底部。只在这块较小的 ROI 内查找
# copy_icon.png，既避免将聊天正文里的相似图形误识别为按钮，也避免对整窗做匹配。
_COPY_ACTION_TOOLBAR_TOP_RATIO = 0.65
_COPY_ICON_MATCH_THRESHOLD = 0.88
_COPY_TEXT = "复制"
_COPY_TOOLBAR_COMPANION_TEXTS = frozenset({
    "逐条转发",
    "合并转发",
    "保存至电脑",
    "收藏",
    "删除",
})

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
def get_copy_action_region(window, QQ_window_image=None, threshold: float | None = None) -> dict | None:
    """定位 QQ 多消息选择工具栏中的“复制”图标。

    返回值是可直接传给鼠标层的**屏幕绝对坐标矩形**；找不到则返回 ``None``。
    这里刻意不使用 OCR：``copy_icon.png`` 是真正可点击的图标，模板匹配只扫描
    窗口底部工具栏 ROI，避免 OCR 的模型推理开销和“复制”文字误命中。

    ``QQ_window_image`` 可由调用方传入同一帧截图，避免为了定位按钮再次调用 WGC。
    """
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    if QQ_window_image is None:
        return None

    # 选择态操作栏在窗口下方；裁剪后得到的坐标仍是相对 ROI 的，需要在下方加回
    # toolbar_top 才能恢复为相对整窗截图的坐标。
    toolbar_top = round(QQ_window_image.height * _COPY_ACTION_TOOLBAR_TOP_RATIO)
    toolbar_image = QQ_window_image.crop((0, toolbar_top, QQ_window_image.width, QQ_window_image.height))
    copy_icon = load_image("copy_icon.png")
    matched = find_template(
        toolbar_image,
        copy_icon,
        threshold=_COPY_ICON_MATCH_THRESHOLD if threshold is None else threshold,
        # 模板与当前 QQ 截图均来自同一 DPI 下时，固定尺寸匹配更快；若后续支持多
        # DPI 模板，可将这里改为 True，而无需改变调用层契约。
        multiscale=False,
    )
    if matched is None:
        return None

    image_region = {
        "x": matched["x"],
        "y": matched["y"] + toolbar_top,
        "left": matched["left"],
        "top": matched["top"] + toolbar_top,
        "right": matched["right"],
        "bottom": matched["bottom"] + toolbar_top,
    }
    return _to_screen(window, image_region)


def _normalise_ocr_text(text: object) -> str:
    """去掉 OCR 文本中的空白，便于匹配工具栏动作名称。"""
    return "".join(str(text or "").split())


def _ocr_box_bounds(box: object) -> tuple[int, int, int, int] | None:
    """把 RapidOCR 的四点框转换成相对当前 ROI 的整数边界。"""
    try:
        points = list(box)  # type: ignore[arg-type]
        xs = [int(round(float(point[0]))) for point in points]
        ys = [int(round(float(point[1]))) for point in points]
    except (TypeError, ValueError, IndexError):
        return None
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _find_copy_text_region(
    ocr_lines: list,
    *,
    image_width: int,
    image_height: int,
    toolbar_top: int,
) -> dict | None:
    """从底部工具栏 OCR 结果中定位文字按钮“复制”。

    特殊会话可能使用纯文字工具栏，而不是 ``copy_icon.png``。这里只接受底部 ROI 内
    的精确“复制”文字，并优先选择与转发/收藏/删除等动作处于同一行的候选，避免把
    聊天正文中的“复制”误认为工具栏按钮。
    """
    toolbar_height = max(0, image_height - toolbar_top)
    recognised: list[tuple[tuple[int, int, int, int], str]] = []
    for line in ocr_lines:
        if len(line) < 2:
            continue
        bounds = _ocr_box_bounds(line[0])
        if bounds is None:
            continue
        recognised.append((bounds, _normalise_ocr_text(line[1])))

    # OCR 返回顺序不保证从左到右。先完整收集同行动作，再按候选复制按钮评分，
    # 避免“复制”出现在列表前面时 companion_count 恒为 0。
    companion_boxes = [
        bounds
        for bounds, text in recognised
        if text in _COPY_TOOLBAR_COMPANION_TEXTS
    ]
    candidates: list[tuple[tuple[int, int], dict]] = []
    for bounds, text in recognised:
        if text != _COPY_TEXT:
            continue
        left, top, right, bottom = bounds
        if bottom <= 0 or top >= toolbar_height:
            continue
        left = max(0, min(left, image_width))
        right = max(0, min(right, image_width))
        top = max(0, min(top, toolbar_height))
        bottom = max(0, min(bottom, toolbar_height))
        if right <= left or bottom <= top:
            continue
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2
        row_tolerance = max(18, (bottom - top) * 2)
        companion_count = sum(
            1
            for comp_left, comp_top, comp_right, comp_bottom in companion_boxes
            if comp_left < image_width
            and comp_right > 0
            and abs(((comp_top + comp_bottom) // 2) - center_y) <= row_tolerance
        )
        region = {
            "x": left,
            "y": top + toolbar_top,
            "w": right - left,
            "h": bottom - top,
            "left": left,
            "top": top + toolbar_top,
            "right": right,
            "bottom": bottom + toolbar_top,
        }
        candidates.append(((companion_count, center_x), region))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _copy_text_button_click_region(
    label_region: dict,
    *,
    image_width: int,
    image_height: int,
) -> dict:
    """把 OCR 文字标签换算为工具栏按钮的图标点击区域。

    QQ 多选工具栏的文字标签本身不是独立点击目标；同一按钮的图标位于文字上方。
    这里按标签尺寸推导按钮图标中心，避免点击标签后什么都不发生。
    """
    text_left = int(label_region["left"])
    text_top = int(label_region["top"])
    text_width = max(1, int(label_region["right"]) - text_left)
    text_height = max(1, int(label_region["bottom"]) - text_top)
    center_x = text_left + text_width // 2

    click_width = max(28, min(60, round(text_width * 1.1)))
    click_height = max(32, min(48, round(text_height * 1.8)))
    gap = max(12, round(text_height * 0.9))

    click_bottom = max(click_height, text_top - gap)
    click_top = click_bottom - click_height
    click_left = center_x - click_width // 2

    click_left = max(0, min(click_left, max(0, image_width - click_width)))
    click_top = max(0, min(click_top, max(0, image_height - click_height)))
    click_right = min(image_width, click_left + click_width)
    click_bottom = min(image_height, click_top + click_height)
    return {
        "x": click_left,
        "y": click_top,
        "w": click_right - click_left,
        "h": click_bottom - click_top,
        "left": click_left,
        "top": click_top,
        "right": click_right,
        "bottom": click_bottom,
    }


@timer
def get_copy_action_region_by_text(window, QQ_window_image=None) -> dict | None:
    """模板未命中时，用 OCR 定位特殊会话工具栏中的文字按钮“复制”。"""
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    if QQ_window_image is None:
        return None

    toolbar_top = round(QQ_window_image.height * _COPY_ACTION_TOOLBAR_TOP_RATIO)
    toolbar_image = QQ_window_image.crop((0, toolbar_top, QQ_window_image.width, QQ_window_image.height))

    # OCR 初始化较重，且仅在图标模板未命中时才调用，因此延迟 import。
    import numpy as np
    from ..vision.ocr import OCREngine

    ocr_lines = OCREngine().ocr(np.array(toolbar_image)) or []
    label_region = _find_copy_text_region(
        ocr_lines,
        image_width=QQ_window_image.width,
        image_height=QQ_window_image.height,
        toolbar_top=toolbar_top,
    )
    if label_region is None:
        return None
    image_region = _copy_text_button_click_region(
        label_region,
        image_width=QQ_window_image.width,
        image_height=QQ_window_image.height,
    )
    return _to_screen(window, image_region)


def _cached_region_result(name: str):
    """缓存区域坐标；命中时仍使用当前整窗截图重新裁剪。"""
    def decorator(func):
        @wraps(func)
        def wrapper(window, QQ_window_image=None):
            if QQ_window_image is None:
                QQ_window_image, _ = get_qq_window_image(window)
            cached = get_region(name, window, QQ_window_image)
            if cached is not None:
                region = cached["region"]
                return RegionResult(
                    screen_region=_to_screen(window, region),
                    image_region=region,
                    image=crop_region(QQ_window_image, region),
                    full_image=QQ_window_image,
                    send_button_region=cached.get("send_button_region"),
                )
            result = func(window, QQ_window_image)
            save_region(
                name,
                window,
                QQ_window_image,
                {"region": result.image_region, "send_button_region": result.send_button_region},
            )
            return result
        return wrapper
    return decorator

_QQ_NAVIGATION_BAR_WIDTH = 60


def _fallback_user_list_region(image: Image.Image, right_top_region: dict) -> dict:
    """左锚点失配时，以右锚点为右界，并固定排除最左侧 60px 导航栏。"""
    left = min(_QQ_NAVIGATION_BAR_WIDTH, int(right_top_region["left"]))
    top = int(right_top_region["bottom"])
    return {
        "x": left, "y": top, "top": top, "bottom": image.height,
        "left": left, "right": int(right_top_region["right"]),
    }


@_cached_region_result("userlist")
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

    if right_top_region is None:
        raise RuntimeError(
            f"get_userList_region_and_image failed: left_top={left_top_region is not None} right_top=False")

    # 新版/主题化 QQ 的搜索栏左侧外观可能变化。右锚点仍有效时，以它确定
    # 右边界和顶部，左边界固定排除最左侧 60px 导航栏。
    if left_top_region is None:
        region = _fallback_user_list_region(QQ_window_image, right_top_region)
        print(
            "[WARN] 用户列表左搜索栏模板未命中，按固定导航栏宽度回退 "
            f"left={region['left']} right={region['right']} top={region['top']}"
        )
    else:
        left = int(left_top_region["left"])
        top = max(int(left_top_region["bottom"]), int(right_top_region["bottom"]))

    if left_top_region is not None:
        region = {"x": left, "y": top, "top": top, "bottom": QQ_window_image.height,
                  "left": left, "right": right_top_region['right']}
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


_INPUT_BOX_BOTTOM_SAFE_MARGIN = 70


def _resolve_input_box_region(
    image: Image.Image, left_top_region: dict, right_top_region: dict | None,
    right_bottom_region: dict | None, *, window_top: int = 0,
) -> dict:
    """生成安全输入区域；新样式发送按钮失配时避开底部按钮行。"""
    top = max(
        int(left_top_region["bottom"]),
        int(right_top_region["bottom"]) if right_top_region is not None else int(left_top_region["bottom"]),
    )
    right = int(right_top_region["right"]) if right_top_region is not None else image.width
    if right_bottom_region is not None:
        bottom = int(right_bottom_region["top"]) - int(window_top) - 30
    else:
        bottom = image.height - _INPUT_BOX_BOTTOM_SAFE_MARGIN
    return {
        "x": int(left_top_region["x"]), "y": top, "top": top,
        "bottom": max(top + 1, bottom), "left": int(left_top_region["left"]), "right": right,
    }


@_cached_region_result("inputbox")
@timer
def get_inputbox_region_and_image(window, QQ_window_image=None):
    """定位输入框区域：输入框左上角 → 发送按钮上方。返回 RegionResult（含发送按钮坐标）。"""
    # 未传截图时现截
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    # 左上角定位：输入框左上角图标（模板）
    inputbox_top_left = load_image("inputbox_top_left.png")
    left_top_region = find_template(QQ_window_image, inputbox_top_left, 0.9)
    if left_top_region is None:
        left_top_region = find_template(QQ_window_image, inputbox_top_left, 0.8)
    # 右上角定位：输入框右上角图标（模板）
    inputbox_top_right = load_image("inputbox_top_right1.png")
    right_top_region = find_template(QQ_window_image, inputbox_top_right, 0.9)
    if right_top_region is None:  # 没找到,还有第二个图标
        # 第一套右上角模板找不到 → 试第二套（界面小差异）
        inputbox_top_right = load_image("inputbox_top_right2.png")
        right_top_region = find_template(QQ_window_image, inputbox_top_right, 0.9)

    # 底部锚点：发送按钮位置（输入框底边就在按钮上方）
    right_bottom_region = get_input_buttom_region(window, QQ_window_image)

    # 左上锚点必须存在；右侧栏和发送按钮在新样式/私聊中允许缺失。
    if left_top_region is None:
        raise RuntimeError(
            f"get_inputbox_region_and_image failed: left_top=False "
            f"right_top={right_top_region is not None} right_bottom={right_bottom_region is not None}")
    region = _resolve_input_box_region(
        QQ_window_image, left_top_region, right_top_region, right_bottom_region, window_top=window.top
    )
    if right_top_region is None:
        print(f"[WARN] 输入区右上锚点未命中，使用窗口右边界 right={region['right']}")
    if right_bottom_region is None:
        print(f"[WARN] 发送按钮模板未命中，输入区避开底部 {_INPUT_BOX_BOTTOM_SAFE_MARGIN}px")
    return RegionResult(
        screen_region=_to_screen(window, region),
        image_region=region,
        image=crop_region(QQ_window_image, region),
        full_image=QQ_window_image,
        # 附带发送按钮屏幕坐标（点击发送时直接用，不必再匹配一次）
        send_button_region=right_bottom_region,
    )


def _resolve_message_box_region(
    image: Image.Image, left_bottom_region: dict, right_bottom_region: dict | None, top_region: dict
) -> dict:
    """根据消息区锚点生成区域；私聊无右侧栏时延伸到窗口右边。"""
    right = int(right_bottom_region["right"]) if right_bottom_region is not None else image.width
    return {
        "x": int(left_bottom_region["x"]), "y": int(top_region["y"]),
        "top": int(top_region["bottom"]), "bottom": int(left_bottom_region["top"]),
        "left": int(left_bottom_region["left"]), "right": right,
    }


@_cached_region_result("messagebox")
@timer
def get_message_box_region_and_image(window, QQ_window_image=None):
    """定位消息框区域：消息框左上角 → 输入框上方。返回 RegionResult。"""
    # 未传截图时现截
    if QQ_window_image is None:
        QQ_window_image, _ = get_qq_window_image(window)
    # 左下角定位：复用输入框左上角图标模板（输入框与消息区共用同款角标）
    messagebox_bottom_left = load_image("inputbox_top_left.png")
    left_bottom_region = find_template(QQ_window_image, messagebox_bottom_left, 0.9)
    if left_bottom_region is None:
        # 浅色/主题化 QQ 中图标抗锯齿有差异，适度降阈值；搜索范围仍由完整模板约束。
        left_bottom_region = find_template(QQ_window_image, messagebox_bottom_left, 0.8)
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
    if top_region is None:
        top_region = find_template(QQ_window_image, messagebox_top, 0.8)

    # 左下锚点给出消息区左边与输入框分界；顶部锚点给出标题栏下沿。
    # 私聊界面没有群成员侧栏，因此右下模板可能不存在，此时消息区自然延伸到窗口右边。
    if left_bottom_region is None or top_region is None:
        raise RuntimeError(
            f"get_message_box_region_and_image failed: left_bottom={left_bottom_region is not None} "
            f"right_bottom={right_bottom_region is not None} top={top_region is not None}")
    region = _resolve_message_box_region(
        QQ_window_image, left_bottom_region, right_bottom_region, top_region
    )
    if right_bottom_region is None:
        print(f"[WARN] 消息区右下锚点未命中，使用窗口右边界 right={region['right']}")
    return RegionResult(
        screen_region=_to_screen(window, region),
        image_region=region,
        image=crop_region(QQ_window_image, region),
        full_image=QQ_window_image,
    )
