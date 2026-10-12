"""QQ 联系人分屏滚动扫描：按 QQ 号精确查找，不使用昵称 OCR。"""

from __future__ import annotations

import logging
from statistics import median
import time
from typing import TYPE_CHECKING

import numpy as np

from ..core.mouse import scroll_at
from ..core.window import WindowCaptureCtx

if TYPE_CHECKING:
    from .user_list import UserList

CONTACT_SCROLL_WAIT_SECONDS = 2.0
CONTACT_SCAN_MAX_PAGES = 20
CONTACT_SCAN_TIMEOUT_SECONDS = 90.0
logger = logging.getLogger("qq_service.contact_scan")


class ContactNotFoundError(ValueError):
    """滚动查找后仍没有目标联系人的可公开业务错误。"""

    def __init__(self, qq_number: str) -> None:
        self.qq_number = qq_number
        super().__init__(f"QQ 用户列表滚动查找后未找到 QQ 号「{qq_number}」")


def _avatar_fingerprint(image, row) -> tuple:
    """用同一截图中的头像像素生成行身份指纹，仅用于判断真实滚动。"""
    x, y, radius = row.avatar
    if radius <= 0:
        return ()
    box = (max(0, x - radius), max(0, y - radius),
           min(image.width, x + radius), min(image.height, y + radius))
    if box[0] >= box[2] or box[1] >= box[3]:
        return ()
    avatar = image.crop(box).convert("RGB").resize((12, 12))
    return tuple(int(value) // 16 for value in np.asarray(avatar).reshape(-1))


class ContactScanner:
    """由 Dispatcher 串行调用，UserList 仍只保存当前屏幕的用户坐标。"""

    def __init__(self, user_list: UserList):
        self.user_list = user_list
        # 最近一次读到的页；扫描刚读过的那一页直接复用，不重复逐行复制号码。
        self._page = None

    def _scroll_list(self, delta: int) -> None:
        """只在已识别的联系人区域滚动，不点击、不切换会话。"""
        region = self.user_list.userList_region.screen_region
        x = int((region["left"] + region["right"]) / 2)
        y = int((region["top"] + region["bottom"]) / 2)
        with WindowCaptureCtx(self.user_list.hwnd):
            scroll_at(x, y, delta)
            time.sleep(CONTACT_SCROLL_WAIT_SECONDS)

    def _scan_context(self) -> tuple:
        region = self.user_list.userList_region
        return (self.user_list.hwnd, tuple(sorted(region.screen_region.items())),
                tuple(sorted(region.image_region.items())), region.full_image.size)

    def _read_page(self):
        """截取当前页、逐行复制 QQ 号一次，并返回行指纹与激活行。"""
        from .contact_number import ContactNumberReader, _active_row, visible_rows
        from .window_ops import ensure_qq_window_with_retry

        window = ensure_qq_window_with_retry()
        reader = ContactNumberReader()
        reader._generation = None
        with WindowCaptureCtx(window._hWnd):
            # 停离鼠标指针后再取基准帧：WGC 会把指针一起采进画面。
            region = reader.capture_region(window)
        rows = visible_rows(region)
        # 激活行只用于快照标注；当前会话未选中时不应中断扫描。
        try:
            active_row = _active_row(region)
        except Exception:
            active_row = None
        numbers: list[tuple[str, object]] = []
        fingerprints: list[tuple[tuple, int]] = []
        for row in rows:
            fingerprint = _avatar_fingerprint(region.image, row)
            if fingerprint:
                fingerprints.append((fingerprint, row.avatar[1]))
            number = reader.copy_row(window, region, row, require_active=False)
            if number is not None:
                numbers.append((number, row))
        self._page = (numbers, fingerprints, region, active_row)
        return self._page

    def read_visible(self) -> dict[str, dict]:
        """枚举当前可见联系人，返回以精确 QQ 号为 key 的快照；不滚动列表。

        刚做过扫描时直接复用那一页：扫描结束时列表没有滚动，页面内容与快照一致。
        """
        from .contact_number import _row_image, _same_row_geometry
        from .user_list import UserList

        if self._page is None:
            self._read_page()
        numbers, _fingerprints, region, active_row = self._page
        snapshot: dict[str, dict] = {}
        for number, row in numbers:
            active = active_row is not None and _same_row_geometry(row, active_row)
            new_msg = UserList.check_new_msg(np.array(_row_image(region, row)))
            snapshot[number] = {
                "avatar": list(row.avatar),
                "rect": list(row.rect),
                "active": active,
                "new_msg": new_msg,
            }
        return snapshot

    @staticmethod
    def _displacement(previous, current):
        """按共同头像指纹返回 (中位位移, 共同数量)。"""
        if not previous or not current:
            return 0.0, 0
        current_by_fingerprint = {}
        for fingerprint, y in current:
            current_by_fingerprint.setdefault(fingerprint, []).append(y)
        shifts = []
        for fingerprint, previous_y in previous:
            candidates = current_by_fingerprint.get(fingerprint)
            if not candidates:
                continue
            current_y = min(candidates, key=lambda value: abs(value - previous_y))
            shifts.append(current_y - previous_y)
        if len(shifts) < 2:
            return 0.0, len(shifts)
        return float(median(shifts)), len(shifts)

    def scan(self, qq_number: str):
        """按 QQ 号查找：先向上确认顶部，再向下逐屏精确匹配。"""
        started = time.monotonic()
        context = self._scan_context()
        gathered: set[str] = set()
        previous_fingerprints = None
        previous_positions: list[int] | None = None
        scroll_delta = 120
        pages = 0
        found = False
        try:
            for pages in range(1, CONTACT_SCAN_MAX_PAGES + 1):
                if self._scan_context() != context:
                    raise RuntimeError("QQ_CONTACT_SCAN_CHANGED")
                if time.monotonic() - started > CONTACT_SCAN_TIMEOUT_SECONDS:
                    raise RuntimeError("QQ_CONTACT_SCAN_LIMIT")

                numbers, fingerprints, _, _ = self._read_page()
                matches = [(number, row) for number, row in numbers if number == qq_number]
                if matches:
                    if len(matches) != 1:
                        raise RuntimeError("QQ_CONTACT_SCAN_CHANGED")
                    found = True
                    return matches[0][1]
                gathered.update(number for number, _ in numbers)

                positions = sorted(y for _, y in fingerprints)
                displacement, common_count = self._displacement(previous_fingerprints, fingerprints)
                stationary = (previous_positions is not None and positions == previous_positions and
                              common_count >= 2 and abs(displacement) <= 2)
                if stationary:
                    if scroll_delta < 0:
                        raise ContactNotFoundError(qq_number)
                    scroll_delta = -120
                elif common_count >= 2 and abs(displacement) > 2:
                    spacing = median([b - a for a, b in zip(positions, positions[1:])]) if len(positions) > 1 else 0
                    height = self.user_list.userList_region.image.height
                    denominator = max(1.0, abs(displacement))
                    step = min(6, max(1, int((height - 2 * spacing) / denominator * abs(scroll_delta) / 120)))
                    scroll_delta = (120 if scroll_delta > 0 else -120) * step
                previous_fingerprints = fingerprints
                previous_positions = positions
                if pages < CONTACT_SCAN_MAX_PAGES:
                    self._scroll_list(scroll_delta)
            raise RuntimeError("QQ_CONTACT_SCAN_LIMIT")
        except Exception as error:
            logger.warning("contacts.scan.failed pages=%d code=%s", pages,
                           type(error).__name__ if isinstance(error, ContactNotFoundError) else str(error))
            raise
        finally:
            logger.info("contacts.scan.complete pages=%d count=%d found=%s duration_ms=%.2f",
                        pages, len(gathered), found, (time.monotonic() - started) * 1000)
