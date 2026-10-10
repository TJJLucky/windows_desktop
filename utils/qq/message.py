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
from pathlib import Path

from PIL import ImageChops, ImageStat
from typing import Any

from .copied_messages import parse_copied_messages
from .regions import (
    get_copy_action_region,
    get_copy_action_region_by_text,
    get_message_box_region_and_image,
)
from .user_list import UserList
from .window_ops import ensure_qq_window_with_retry, get_qq_window_image
from ..core.mouse import click_at, drag, find_non_resize_x
from ..core.timing import timer
from ..core.window import WindowCaptureCtx


# QQ 的 Chromium 消息区需要收到多次 mouse move 才会进入框选态；单次瞬时移动
# 偶发只触发普通点击，因此保留一条很短的线性轨迹。
_MESSAGE_SELECTION_DRAG_DURATION = 0.2

# 空会话没有消息气泡，聊天画布保持 QQ 的浅灰背景。只检查消息区左侧固定宽度，
# 避免将右侧联系人资料栏或群成员栏误纳入判断。
_EMPTY_CHAT_BACKGROUND = (245, 245, 245)
_EMPTY_CHAT_SAMPLE_WIDTH = 384
_EMPTY_CHAT_BACKGROUND_RATIO = 0.995


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


def _is_empty_message_region(window: Any, image, region: dict[str, int]) -> bool:
    """判断消息区是否为无消息的纯 ``#f5f5f5`` 画布。

    真实 QQ 空会话的聊天画布为纯 ``#f5f5f5``；右侧资料栏不属于消息正文区，
    因此只采样消息区左侧固定宽度，并要求几乎全部像素都是该背景色。
    """
    if image is None:
        return False
    left = max(0, int(region["left"]) - int(window.left))
    top = max(0, int(region["top"]) - int(window.top))
    right = min(
        image.width,
        int(region["right"]) - int(window.left),
        left + _EMPTY_CHAT_SAMPLE_WIDTH,
    )
    bottom = min(image.height, int(region["bottom"]) - int(window.top))
    if right <= left or bottom <= top:
        return False

    crop = image.crop((left, top, right, bottom))
    if crop.mode != "RGB":
        crop = crop.convert("RGB")
    total = crop.width * crop.height
    background = sum(1 for pixel in crop.getdata() if pixel == _EMPTY_CHAT_BACKGROUND)
    return background / total >= _EMPTY_CHAT_BACKGROUND_RATIO


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
                if copy_action is None:
                    region = self._refresh_message_region(main_window)
                    # 区域上边是半开区间边界，+1 取真实内容第一像素；左边界则
                    # 按当前光标命中区动态探测，不能使用固定内边距。QQ 的左侧
                    # 分隔条命中区会把正常拖动变成调整布局。
                    selection_top = region["top"] + 1
                    selection_left = find_non_resize_x(
                        region["left"],
                        selection_top,
                        region["right"] - 1,
                    )
                    if selection_left is None:
                        raise RuntimeError("QQ_MESSAGE_SELECTION_START_NOT_FOUND")
                    selection_start = (selection_left, selection_top)
                    selection_end = (region["right"] - 1, region["bottom"] - 1)
                    metadata.update(
                        messageRegion=region,
                        dragHitStart=(region["left"], selection_top),
                        dragFrom=selection_start,
                        dragTo=selection_end,
                    )
                    if _is_empty_message_region(main_window, before, region):
                        metadata["emptyConversation"] = True
                        return ""
                    if selection_start[0] >= selection_end[0] or selection_start[1] >= selection_end[1]:
                        raise RuntimeError("QQ_MESSAGE_REGION_TOO_SMALL")
                    drag(*selection_start, *selection_end, duration=_MESSAGE_SELECTION_DRAG_DURATION)
                    deadline = time.perf_counter() + 2.0
                    while time.perf_counter() < deadline:
                        time.sleep(0.15)
                        after, window = get_qq_window_image(main_window)
                        if after is None:
                            continue
                        search_image = _copy_search_image(after)
                        copy_action = get_copy_action_region(window, after, threshold=0.88)
                        if copy_action is None:
                            copy_action = get_copy_action_region(window, after, threshold=0.80)
                        if copy_action is not None:
                            break
                    if copy_action is None:
                        # 部分临时会话/特殊聊天使用纯文字工具栏，没有 copy_icon.png。
                        # 只在模板等待失败后做一次 OCR，避免普通路径承担额外耗时。
                        if after is None:
                            after, window = get_qq_window_image(main_window)
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

        返回消息仅包含 sender、timestamp、text 和 rawText；方向由外部智能体根据
        sender 自行判断，服务不从气泡位置推断收发方向。
        """
        UserList().active_user_by_name(contact_name)
        copied_text = self.copy_visible_selection()
        if not copied_text:
            return {"copiedText": None, "messages": []}
        try:
            messages = parse_copied_messages(copied_text, fallback_sender=contact_name)
        except Exception as exc:
            raise RuntimeError("QQ_COPY_PARSE_FAILED") from exc
        if not messages:
            raise RuntimeError("QQ_COPY_PARSE_FAILED")
        return {"copiedText": copied_text, "messages": messages}
