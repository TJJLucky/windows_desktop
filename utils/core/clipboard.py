"""独立复制操作的剪贴板标记与有界读取；不包含 QQ 业务判断。"""
from __future__ import annotations

import time
import uuid

import win32clipboard
import win32process


class ClipboardCopyError(RuntimeError):
    pass


class ClipboardCopy:
    """每次复制独立标记，防止把原有文本当成本次结果。"""

    def __init__(self):
        self.marker = f"QQ_CONTACT_COPY_{uuid.uuid4().hex}"
        self.sequence = 0

    def mark(self):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, self.marker)
                self.sequence = win32clipboard.GetClipboardSequenceNumber()
            finally:
                win32clipboard.CloseClipboard()
        except Exception as exc:
            raise ClipboardCopyError("CLIPBOARD_UNAVAILABLE") from exc

    def read(self, timeout: float = 2.0, *, expected_pid: int | None = None) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            opened = False
            try:
                win32clipboard.OpenClipboard()
                opened = True
                if win32clipboard.GetClipboardSequenceNumber() != self.sequence:
                    if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                        raise ClipboardCopyError("CLIPBOARD_TEXT_UNAVAILABLE")
                    value = str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or "")
                    if value != self.marker:
                        owner = win32clipboard.GetClipboardOwner()
                        if expected_pid is not None and (not owner or win32process.GetWindowThreadProcessId(owner)[1] != expected_pid):
                            raise ClipboardCopyError("CLIPBOARD_OWNER_CHANGED")
                        return value
            except ClipboardCopyError:
                raise
            except Exception:
                # 短暂被占用时仅等待同一次复制结果，不重新点击。
                pass
            finally:
                if opened:
                    win32clipboard.CloseClipboard()
            time.sleep(0.05)
        raise ClipboardCopyError("CLIPBOARD_UNCHANGED")
