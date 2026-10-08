"""QQ 当前可见消息读取：原生框选、复制并解析剪贴板文本。

消息内容不再使用气泡检测或 OCR。``MessageList`` 负责完整的 QQ UI 操作：
激活会话、定位消息区域、左上到右下框选、点击复制按钮并读取剪贴板；随后把
剪贴板文本解析为发送人、时间和正文。联系人列表本身仍由 ``UserList`` 负责识别。
"""

from __future__ import annotations

import time
from typing import Any

from .copied_messages import parse_copied_messages
from .regions import get_copy_action_region, get_message_box_region_and_image
from .user_list import UserList
from .window_ops import ensure_qq_window_with_retry, get_qq_window_image
from ..core.mouse import click_at, drag
from ..core.timing import timer
from ..core.window import WindowCaptureCtx


# 左上到右下的真实 QQ 多选测试中，0.0 秒连续 10 次均命中 copy_icon.png。
_MESSAGE_SELECTION_DRAG_DURATION = 0.0


def _read_unicode_clipboard(retries: int = 5, delay: float = 0.1) -> str:
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
                    if text.strip():
                        return text
                    last_error = RuntimeError("QQ_COPY_CLIPBOARD_EMPTY")
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:
            last_error = exc
            time.sleep(delay)
            continue
        time.sleep(delay)
    raise RuntimeError("QQ_COPY_CLIPBOARD_UNAVAILABLE") from last_error


def _clear_clipboard(retries: int = 5, delay: float = 0.1) -> None:
    """在点击“复制”前清空剪贴板，避免旧文本被误作本次结果。"""
    import win32clipboard

    last_error: Exception | None = None
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                return
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:
            last_error = exc
            time.sleep(delay)
    raise RuntimeError("QQ_COPY_CLIPBOARD_CLEAR_FAILED") from last_error


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
        """框选当前可见消息、点击 QQ 的复制按钮并读取剪贴板原文。

        QQ 始终维持顶层；若当前已处于多选状态，直接定位复制按钮，不会重复拖拽。
        """
        main_window = ensure_qq_window_with_retry()
        self.hwnd = main_window._hWnd
        with WindowCaptureCtx(self.hwnd):
            image, window = get_qq_window_image(main_window)
            if image is None:
                raise RuntimeError("QQ_COPY_CAPTURE_FAILED")
            copy_action = get_copy_action_region(window, image)

            if copy_action is None:
                region = self._refresh_message_region(main_window)
                horizontal_padding = max(40, min(80, region["w"] // 12))
                vertical_padding = max(40, min(80, region["h"] // 12))
                selection_start = (region["left"] + horizontal_padding, region["top"] + vertical_padding)
                selection_end = (region["right"] - horizontal_padding, region["bottom"] - vertical_padding)
                if selection_start[0] >= selection_end[0] or selection_start[1] >= selection_end[1]:
                    raise RuntimeError("QQ_MESSAGE_REGION_TOO_SMALL")

                drag(*selection_start, *selection_end, duration=_MESSAGE_SELECTION_DRAG_DURATION)
                time.sleep(0.25)
                image, window = get_qq_window_image(main_window)
                if image is None:
                    raise RuntimeError("QQ_COPY_CAPTURE_FAILED")
                copy_action = get_copy_action_region(window, image)
                if copy_action is None:
                    raise RuntimeError("QQ_COPY_ACTION_NOT_FOUND")

            _clear_clipboard()
            click_at(
                copy_action["left"] + copy_action["w"] // 2,
                copy_action["top"] + copy_action["h"] // 2,
                restore=False,
                smooth=False,
            )

        time.sleep(0.15)
        copied = _read_unicode_clipboard()
        if not copied.strip():
            raise RuntimeError("QQ_COPY_EMPTY")
        return copied

    def read_messages(self, contact_name: str) -> dict[str, Any]:
        """读取指定会话的可见消息，返回完整复制文本与解析后的消息数组。

        返回消息仅包含 sender、timestamp、text 和 rawText；方向由外部智能体根据
        sender 自行判断，服务不从气泡位置推断收发方向。
        """
        UserList().active_user_by_name(contact_name)
        copied_text = self.copy_visible_selection()
        return {
            "copiedText": copied_text,
            "messages": parse_copied_messages(copied_text),
        }
