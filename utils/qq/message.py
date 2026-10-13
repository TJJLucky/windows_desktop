"""QQ 当前可见消息读取：原生框选、复制并解析剪贴板文本。

消息内容不再使用气泡检测或 OCR。``MessageList`` 负责完整的 QQ UI 操作：
激活会话、定位消息区域、左上到右下框选、点击复制按钮并读取剪贴板；随后把
剪贴板文本解析为发送人、时间和正文。联系人列表本身仍由 ``UserList`` 负责识别。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageStat
from typing import Any

from .copied_messages import parse_copied_messages
from .hotkeys import send_hotkey
from .regions import (
    get_copy_action_region,
    get_copy_action_region_by_text,
    get_message_box_region_and_image,
)
from .user_list import UserList
from .window_ops import ensure_qq_window_with_retry, get_qq_window_image
from ..core.mouse import click_at, drag, find_non_resize_x
from ..core.screenshot import window_dpi
from ..core.timing import timer
from ..core.window import WindowCaptureCtx


# QQ 的 Chromium 消息区需要收到多次 mouse move 才会进入框选态；单次瞬时移动
# 偶发只触发普通点击，因此保留一条很短的线性轨迹。
_MESSAGE_SELECTION_DRAG_DURATION = 0.2

# 空会话没有消息气泡，聊天画布保持 QQ 的浅灰背景（仅作为取不到真实底色时的兜底）。
# 只检查消息区左侧固定宽度，避免将右侧联系人资料栏或群成员栏误纳入判断。
_EMPTY_CHAT_BACKGROUND = (245, 245, 245)
_EMPTY_CHAT_SAMPLE_WIDTH = 384
_EMPTY_CHAT_BACKGROUND_RATIO = 0.995

# 框选几何的基准像素值是在 120 DPI（125% 缩放）机器上真机标定的；其它电脑按窗口
# 所在显示器的实际 DPI 与基准的比值等比缩放。QQ 的分隔条/系统边框命中区、粘性
# 日期栏和气泡左边距都随 DPI 缩放，写死物理像素会在别的缩放比例上把按下点或终点
# 放进这些命中区，从而进不了框选态。
_SELECTION_DPI_BASELINE = 120

# 拖拽矩形相对消息区域向内收缩的像素数（四边基准值）。区域左边界压在消息区与
# 联系人列表之间的分隔条命中区上、下边界压着输入框上边线，私聊没有右侧栏时右边
# 界等于窗口右边界（落在系统尺寸调整边框里）：起点或终点落在这些边界上时 QQ 不会
# 进入消息框选态。基准机上实测左边界需让开 3px 才能按下，右、下边界让开 0~8px
# 都能成功，统一取 8px 留余量。
_SELECTION_INSET_BASE = 8

# 消息区最顶部是 QQ 粘性日期栏的命中区：基准机上实测该命中区约 20~21px 高，按下点
# 落在其中时不会进入框选态（尚未滚动、顶部没有日期栏的会话则几乎立刻可以按下）。
# 这里固定让开一个日期栏高度；仍未进入框选态时按同一步长继续下移重试，最多再试 2 次。
_SELECTION_TOP_DEAD_ZONE_BASE = 22
_SELECTION_TOP_RETRY_LIMIT = 2

# 起点优先落在气泡左侧的空白边距里：自上而下按固定步进逐行寻找一段连续画布底色，
# 横向只在边距宽度内搜索，避免按下点压到头像或气泡而只被当成普通点击。
_SELECTION_GUTTER_WIDTH_BASE = 56
_SELECTION_BLANK_SCAN_STEP_BASE = 2
_SELECTION_BLANK_RUN_BASE = 4

# 拖拽后等待多选工具栏出现的超时与轮询间隔；退出多选态后的稳定等待。
_SELECTION_COPY_WAIT_SECONDS = 2.0
_SELECTION_COPY_POLL_INTERVAL = 0.15
_SELECTION_CLEAR_WAIT_SECONDS = 0.35


@dataclass(frozen=True)
class _SelectionGeometry:
    """一次读取使用的框选几何（已按窗口 DPI 缩放为物理像素）。"""

    inset: int
    top_dead_zone: int
    gutter_width: int
    scan_step: int
    blank_run: int


def _window_dpi_scale(window: Any) -> float:
    """窗口所在显示器的 DPI 缩放比（相对框选几何基准 120 DPI）。

    ``GetDpiForWindow`` 对 per-monitor DPI 感知进程返回窗口所在显示器的实际 DPI；
    取不到时退回 1.0（等于基准机），保证旧环境行为不变。
    """
    dpi = window_dpi(getattr(window, "_hWnd", 0))
    if dpi <= 0:
        return 1.0
    return min(3.0, max(0.5, dpi / _SELECTION_DPI_BASELINE))


def _selection_geometry(window: Any) -> _SelectionGeometry:
    """按窗口 DPI 缩放框选几何基准值；低缩放下仍保底，避免收缩到 0。"""
    scale = _window_dpi_scale(window)
    return _SelectionGeometry(
        inset=max(4, round(_SELECTION_INSET_BASE * scale)),
        top_dead_zone=max(8, round(_SELECTION_TOP_DEAD_ZONE_BASE * scale)),
        gutter_width=max(24, round(_SELECTION_GUTTER_WIDTH_BASE * scale)),
        scan_step=max(1, round(_SELECTION_BLANK_SCAN_STEP_BASE * scale)),
        blank_run=max(2, round(_SELECTION_BLANK_RUN_BASE * scale)),
    )


def _read_unicode_clipboard(sentinel: str, retries: int = 10, delay: float = 0.1) -> str:
    """等待并读取 QQ 点击“复制”后写入的 Unicode 文本。"""
    import win32clipboard

    last_error: Exception | None = None
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            try:
                if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    last_error = RuntimeError("QQ_COPY_CLIPBOARD_NOT_READY")
                else:
                    text = str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or "")
                    if text == sentinel:
                        last_error = RuntimeError("QQ_COPY_CLIPBOARD_UNCHANGED")
                    elif text.strip():
                        return text
                    else:
                        last_error = RuntimeError("QQ_COPY_TEXT_EMPTY")
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:
            last_error = exc
            time.sleep(delay)
            continue
        time.sleep(delay)
    if isinstance(last_error, RuntimeError):
        raise last_error
    raise RuntimeError("QQ_COPY_CLIPBOARD_UNAVAILABLE") from last_error


def _write_clipboard_sentinel(value: str, retries: int = 5, delay: float = 0.1) -> None:
    """写入唯一哨兵，用于确认点击复制后剪贴板确实发生变化。"""
    import win32clipboard

    last_error: Exception | None = None
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, value)
                return
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:
            last_error = exc
            time.sleep(delay)
    raise RuntimeError("QQ_COPY_CLIPBOARD_SENTINEL_FAILED") from last_error


def _diagnostic_root() -> Path:
    cache_db = os.environ.get("QQ_WINDOW_CACHE_DB")
    state_dir = Path(cache_db).expanduser().resolve().parent if cache_db else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "price-agent-qq-service" / "state"
    return state_dir / "logs" / "diagnostics"


def _save_copy_diagnostics(
    *, request_id: str, before, after, search_image, metadata: dict[str, Any]
) -> Path:
    root = _diagnostic_root()
    target = root / request_id
    target.mkdir(parents=True, exist_ok=True)
    if before is not None:
        before.save(target / "window-before-drag.png")
    if after is not None:
        after.save(target / "window-after-drag.png")
    if search_image is not None:
        search_image.save(target / "copy-search-region.png")
    (target / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    directories = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in directories[20:]:
        import shutil
        shutil.rmtree(stale, ignore_errors=True)
    return target


def _copy_search_image(image):
    top = round(image.height * 0.65)
    return image.crop((0, top, image.width, image.height))


def _selection_ui_changed(before, after) -> bool:
    """判断拖拽后底部工具栏区域是否出现了可见变化。"""
    if before is None or after is None or before.size != after.size:
        return True
    difference = ImageChops.difference(_copy_search_image(before), _copy_search_image(after))
    return max(ImageStat.Stat(difference).mean) >= 1.0


def _sample_crop(image, window: Any, region: dict[str, int], width: int):
    """裁出消息区左侧宽度为 ``width`` 的采样条（截图内坐标），越界返回 None。"""
    if image is None:
        return None
    left = max(0, int(region["left"]) - int(window.left))
    top = max(0, int(region["top"]) - int(window.top))
    right = min(image.width, int(region["right"]) - int(window.left), left + width)
    bottom = min(image.height, int(region["bottom"]) - int(window.top))
    if right <= left or bottom <= top:
        return None
    crop = image.crop((left, top, right, bottom))
    if crop.mode != "RGB":
        crop = crop.convert("RGB")
    return crop


def _dominant_color(crop) -> tuple[tuple[int, int, int], float]:
    """返回裁剪图的主色与其占比。

    缩略到最多 64×64 再统计（NEAREST 不改变原色），避免对整块区域逐像素计数。
    """
    width = max(1, min(64, crop.width))
    height = max(1, min(64, crop.height))
    thumb = crop.resize((width, height), Image.NEAREST)
    counts: dict[tuple[int, int, int], int] = {}
    for pixel in thumb.getdata():
        key = tuple(pixel[:3])
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return _EMPTY_CHAT_BACKGROUND, 0.0
    color = max(counts, key=counts.get)
    return color, counts[color] / float(width * height)


def _canvas_background(
    image, window: Any, region: dict[str, int], geometry: _SelectionGeometry
) -> tuple[int, int, int]:
    """消息区画布底色：取气泡/头像之外的左侧边距主色。

    浅色主题下是 ``#f5f5f5``，深色或自定义主题下是别的底色。不写死颜色，才能在
    别的电脑/主题上继续区分“空白画布”和气泡；取不到时退回浅灰默认值。
    """
    crop = _sample_crop(image, window, region, geometry.inset + geometry.blank_run + 4)
    if crop is None:
        return _EMPTY_CHAT_BACKGROUND
    color, ratio = _dominant_color(crop)
    # 边距里偶尔有头像/气泡时占比会下降，此时不可信，退回默认底色。
    return color if ratio >= 0.5 else _EMPTY_CHAT_BACKGROUND


def _is_empty_message_region(
    window: Any, image, region: dict[str, int], background: tuple[int, int, int]
) -> bool:
    """判断消息区是否为无消息的纯色画布。

    真实 QQ 空会话的聊天画布为单一底色；右侧资料栏不属于消息正文区，因此只采样
    消息区左侧固定宽度，并要求几乎全部像素都是该底色。底色按当前主题自适应
    （见 ``_canvas_background``），不再写死浅灰。
    """
    crop = _sample_crop(image, window, region, _EMPTY_CHAT_SAMPLE_WIDTH)
    if crop is None:
        return False
    total = crop.width * crop.height
    matches = sum(1 for pixel in crop.getdata() if tuple(pixel[:3]) == background)
    return matches / total >= _EMPTY_CHAT_BACKGROUND_RATIO


def _is_blank_canvas_run(
    image, x: int, y: int, background: tuple[int, int, int], run: int
) -> bool:
    """判断截图上从 (x, y) 开始的一段像素是否都是空白画布底色。"""
    if image is None:
        return False
    for offset in range(run):
        sample_x = x + offset
        if sample_x < 0 or sample_x >= image.width or y < 0 or y >= image.height:
            return False
        if tuple(image.getpixel((sample_x, y))[:3]) != background:
            return False
    return True


def _blank_canvas_run_x(
    image, y: int, left: int, right: int, background: tuple[int, int, int], run: int
) -> int | None:
    """在截图第 y 行的 [left, right] 区间内寻找第一段连续画布底色的起始 x。

    逐像素比较铺满整段消息区高度会明显拖慢一次读取，这里先把整行转成 RGB 字节串，
    用 C 层的子串查找完成同样的判断；命中后还要校验像素对齐，避免把跨像素的巧合
    匹配当成空白画布。
    """
    if image is None or y < 0 or y >= image.height:
        return None
    span_left = max(0, min(int(left), image.width))
    span_right = max(0, min(int(right) + 1, image.width))
    if span_right - span_left < run:
        return None
    row = image.crop((span_left, y, span_right, y + 1))
    if row.mode != "RGB":
        row = row.convert("RGB")
    data = row.tobytes()
    needle = bytes(background) * run
    offset = 0
    while True:
        index = data.find(needle, offset)
        if index < 0:
            return None
        if index % 3 == 0:
            return span_left + index // 3
        offset = index + 1


def _find_selection_start(
    window: Any,
    image,
    region: dict[str, int],
    geometry: _SelectionGeometry,
    background: tuple[int, int, int],
    top_offset: int,
) -> tuple[int, int, str]:
    """返回让 QQ 进入消息框选态的按下坐标（屏幕坐标）与起点来源。

    按下点必须落在消息区域内的空白画布上：压在区域边界、分隔条命中区、头像或气泡
    上的按下都不会进入多选态，区域顶部 ``top_offset`` 像素内也不行。QQ 的框选不要求
    从左上角起手，但要求拖拽矩形至少覆盖到一条消息，因此这里从顶部命中区下方开始、
    自上而下找第一处空白画布（优先气泡左侧的边距），再从那里向右下拖到右下角。
    """
    window_left = int(window.left)
    window_top = int(window.top)
    region_left = int(region["left"]) + geometry.inset
    region_gutter_right = int(region["left"]) + geometry.inset + geometry.gutter_width
    region_top = int(region["top"]) + top_offset
    region_right = int(region["right"]) - 1 - geometry.inset
    region_bottom = int(region["bottom"]) - 1 - geometry.inset
    if image is not None:
        left = max(0, region_left - window_left)
        gutter_right = min(image.width - 1, region_gutter_right - window_left)
        top = max(0, region_top - window_top)
        bottom = min(image.height - 1, region_bottom - window_top)
        for y in range(top, bottom + 1, geometry.scan_step):
            x = _blank_canvas_run_x(image, y, left, gutter_right, background, geometry.blank_run)
            if x is None:
                continue
            safe_x = find_non_resize_x(window_left + x, window_top + y, region_right)
            if safe_x is None:
                break
            if safe_x == window_left + x:
                return (safe_x, window_top + y, "blank-canvas")
            # 分隔条命中区把起点推开了：在推开后的横坐标上重新确认空白画布。
            if _is_blank_canvas_run(
                image, safe_x - window_left, y, background, geometry.blank_run
            ):
                return (safe_x, window_top + y, "blank-canvas")
    # 兜底：截图上找不到空白画布时，沿用左上角收缩 + 避开尺寸调整命中区的旧策略。
    fallback_left = find_non_resize_x(region_left, region_top, region_right)
    if fallback_left is None:
        raise RuntimeError("QQ_MESSAGE_SELECTION_START_NOT_FOUND")
    return (fallback_left, region_top, "inset-fallback")


def _drag_until_copy_action(main_window: Any, start: tuple[int, int], end: tuple[int, int]):
    """框选拖拽一次并轮询等待多选工具栏的复制按钮出现。

    返回 ``(after, window, copy_action)``；超时未出现时 ``copy_action`` 为 ``None``。
    """
    drag(*start, *end, duration=_MESSAGE_SELECTION_DRAG_DURATION)
    after = window = None
    copy_action = None
    deadline = time.perf_counter() + _SELECTION_COPY_WAIT_SECONDS
    while time.perf_counter() < deadline:
        time.sleep(_SELECTION_COPY_POLL_INTERVAL)
        after, window = get_qq_window_image(main_window)
        if after is None:
            continue
        copy_action = get_copy_action_region(window, after, threshold=0.88)
        if copy_action is None:
            copy_action = get_copy_action_region(window, after, threshold=0.80)
        if copy_action is not None:
            break
    return after, window, copy_action


def _clear_message_selection(main_window: Any) -> None:
    """退出 QQ 的多选态。

    框选成功后 QQ 会一直停留在多选态，底部工具栏顶掉输入框锚点；残留状态下再读取
    会定位不到消息区（真机诊断出现过 left_bottom=False right_bottom=False），也会
    把陈旧选区当成当前会话内容，因此每次读取结束都必须退出多选态。
    """
    send_hotkey(("esc",))
    time.sleep(_SELECTION_CLEAR_WAIT_SECONDS)


class MessageList:
    """QQ 消息读取单例：会话激活、框选复制和剪贴板解析的唯一入口。"""

    _instance: "MessageList | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self.message_screen_region: dict[str, int] | None = None
        self.hwnd: int = 0

    def _refresh_message_region(self, main_window: Any) -> dict[str, int]:
        """取得当前消息区域的屏幕坐标，仅服务于框选范围计算。

        此处截图只用于模板定位消息区域，不进行气泡检测、图像拼接或 OCR。
        """
        try:
            result = get_message_box_region_and_image(main_window)
        except Exception:
            # 多选态的底部工具栏会顶掉输入框锚点，导致消息区定位失败（真机诊断出现
            # 过 left_bottom=False right_bottom=False）：退出多选态后重试一次。
            _clear_message_selection(main_window)
            result = get_message_box_region_and_image(main_window)
        self.message_screen_region = result.screen_region
        self.hwnd = main_window._hWnd
        return result.screen_region

    @timer
    def copy_visible_selection(self) -> str:
        """框选消息，轮询多选工具栏，复制并验证剪贴板结果。"""
        diagnostic_id = f"copy-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        before = after = search_image = None
        metadata: dict[str, Any] = {"requestId": diagnostic_id}
        try:
            main_window = ensure_qq_window_with_retry()
            self.hwnd = main_window._hWnd
            sentinel = f"QQ_DESKTOP_COPY_SENTINEL_{uuid.uuid4().hex}"
            with WindowCaptureCtx(self.hwnd):
                before, window = get_qq_window_image(main_window)
                if before is None:
                    raise RuntimeError("QQ_COPY_CAPTURE_FAILED")
                copy_action = get_copy_action_region(window, before, threshold=0.88)
                copy_action_source = "template"
                if copy_action is not None:
                    # 上一次读取或被人工框选留下的多选态：先退出，保证本次重新框选整段
                    # 可见消息，而不是把一段陈旧选区当成当前会话的消息返回。
                    _clear_message_selection(main_window)
                    before, window = get_qq_window_image(main_window)
                    if before is None:
                        raise RuntimeError("QQ_COPY_CAPTURE_FAILED")
                    copy_action = None
                if copy_action is None:
                    region = self._refresh_message_region(main_window)
                    geometry = _selection_geometry(main_window)
                    selection_end = (
                        region["right"] - 1 - geometry.inset,
                        region["bottom"] - 1 - geometry.inset,
                    )
                    # 区域锚点命中错误时，收缩后可能只剩一条缝：这种情况不再被当成空
                    # 会话静默返回 0 条消息，而是按区域不可用直接报错。
                    if (
                        region["left"] + geometry.inset >= selection_end[0]
                        or region["top"] + geometry.top_dead_zone >= selection_end[1]
                    ):
                        raise RuntimeError("QQ_MESSAGE_REGION_TOO_SMALL")
                    background = _canvas_background(before, main_window, region, geometry)
                    metadata["canvasBackground"] = list(background)
                    metadata["dpiScale"] = round(_window_dpi_scale(main_window), 3)
                    if _is_empty_message_region(main_window, before, region, background):
                        metadata["emptyConversation"] = True
                        return ""
                    after = window = None
                    copy_action = None
                    for attempt in range(_SELECTION_TOP_RETRY_LIMIT + 1):
                        top_offset = geometry.top_dead_zone * (attempt + 1)
                        # 起点落在气泡左侧边距的空白画布上，并向下躲开顶部日期栏命中
                        # 区；仍未进入框选态时把起点整体下移一个守卫高度重试。
                        start_x, start_y, start_source = _find_selection_start(
                            main_window, before, region, geometry, background, top_offset
                        )
                        selection_start = (start_x, start_y)
                        metadata.update(
                            messageRegion=region,
                            dragInset=geometry.inset,
                            dragTopOffset=top_offset,
                            dragAttempt=attempt,
                            dragStartSource=start_source,
                            dragFrom=selection_start,
                            dragTo=selection_end,
                        )
                        after, window, copy_action = _drag_until_copy_action(
                            main_window, selection_start, selection_end
                        )
                        # 已进入框选态但没命中复制按钮时保留该截图：二次拖拽会清掉选区，
                        # 反而把“已进框选、只是没找到按钮”误报成“未进入框选态”。
                        if copy_action is not None or _selection_ui_changed(before, after):
                            break
                        _clear_message_selection(main_window)
                    if copy_action is None:
                        # 部分临时会话/特殊聊天使用纯文字工具栏，没有 copy_icon.png。
                        # 只在模板等待失败后做一次 OCR，避免普通路径承担额外耗时。
                        if after is None:
                            after, window = get_qq_window_image(main_window)
                        if after is not None and search_image is None:
                            search_image = _copy_search_image(after)
                        if after is not None:
                            copy_action = get_copy_action_region_by_text(window, after)
                            if copy_action is not None:
                                copy_action_source = "ocr-text"
                        if copy_action is None:
                            if not _selection_ui_changed(before, after):
                                raise RuntimeError("QQ_SELECTION_NOT_ENTERED")
                            raise RuntimeError("QQ_COPY_ACTION_NOT_FOUND")
                metadata["copyAction"] = copy_action
                metadata["copyActionSource"] = copy_action_source
                # 确认复制按钮存在后再覆盖剪贴板；定位失败不能破坏用户原剪贴板。
                _write_clipboard_sentinel(sentinel)
                click_at(
                    copy_action["left"] + copy_action["w"] // 2,
                    copy_action["top"] + copy_action["h"] // 2,
                    restore=False, smooth=False,
                )
                # 复制完成后立刻退出多选态：残留的工具栏会顶掉输入框锚点，使下一次读取
                # 定位不到消息区。
                _clear_message_selection(main_window)
            copied = _read_unicode_clipboard(sentinel)
            if not copied.strip():
                raise RuntimeError("QQ_COPY_TEXT_EMPTY")
            return copied
        except Exception as exc:
            metadata["failureCode"] = str(exc)
            try:
                path = _save_copy_diagnostics(
                    request_id=diagnostic_id, before=before, after=after,
                    search_image=search_image, metadata=metadata,
                )
                metadata["diagnosticPath"] = str(path)
            except Exception:
                pass
            raise

    def read_messages(self, contact_name: str) -> dict[str, Any]:
        """读取指定会话的可见消息，返回完整复制文本与解析后的消息数组。

        读取仍走 QQ 原生框选复制：先按会话名在无障碍树里激活目标会话，再框选消息区
        并点击复制按钮。返回消息仅包含 sender、timestamp、text 和 rawText；
        方向由外部智能体根据 sender 自行判断，服务不从气泡位置推断收发方向。

        激活后先确认目标会话有复制项：QQ 对"我的手机""QQ邮箱提醒"这类服务/系统会话
        禁用了框选复制（真机实测复制按钮呈斜杠禁用态、剪贴板不变），这类会话已在会话
        名单里被筛掉；这里再挡一次，把 4 秒的框选失败变成毫秒级的 QQ_CONTACT_NOT_COPYABLE。
        """
        # 延迟导入：contact_lookup 依赖无障碍树（UIA/COM），保证本模块可独立加载
        from .contact_lookup import ensure_message_copy_supported, record_refused_copy

        UserList().activate(contact_name)
        ensure_message_copy_supported(contact_name)
        try:
            copied_text = self.copy_visible_selection()
        except RuntimeError as exc:
            # QQ 明确拒绝复制的会话（框选进不了多选态、剪贴板始终不变等）：按昵称记进
            # state 目录，之后从名单里筛掉并直接报 QQ_CONTACT_NOT_COPYABLE，不再重跑
            # 这条十几秒的失败路径。原错误照常抛给调用方。
            record_refused_copy(contact_name, str(exc))
            raise
        if not copied_text:
            return {"copiedText": None, "messages": []}
        try:
            messages = parse_copied_messages(copied_text, fallback_sender=contact_name)
        except Exception as exc:
            raise RuntimeError("QQ_COPY_PARSE_FAILED") from exc
        if not messages:
            raise RuntimeError("QQ_COPY_PARSE_FAILED")
        return {"copiedText": copied_text, "messages": messages}
