"""QQ 联系人分屏滚动扫描：复用单屏截图/OCR，不持有可点击的跨屏坐标。"""

from __future__ import annotations

import logging
from statistics import median
import time
from typing import TYPE_CHECKING

from ..core.mouse import scroll_at
from ..core.window import WindowCaptureCtx

if TYPE_CHECKING:
    from .user_list import UserList

CONTACT_SCROLL_WAIT_SECONDS = 2.0
CONTACT_SCAN_MAX_PAGES = 20
CONTACT_SCAN_TIMEOUT_SECONDS = 40.0
logger = logging.getLogger("qq_service.contact_scan")


class ContactNotFoundError(ValueError):
    """滚动查找后仍没有目标联系人的可公开业务错误。"""

    def __init__(self, contact_name: str) -> None:
        self.contact_name = contact_name
        super().__init__(f"QQ 用户列表滚动查找后未找到联系人「{contact_name}」")


class ContactScanner:
    """由 Dispatcher 串行调用，UserList 仍只保存当前屏幕的用户坐标。"""

    def __init__(self, user_list: UserList):
        self.user_list = user_list

    def _scroll_list(self, delta: int) -> None:
        """只在已识别的联系人区域滚动，不点击、不切换会话。"""
        region = self.user_list.userList_region.screen_region
        x = int((region["left"] + region["right"]) / 2)
        y = int((region["top"] + region["bottom"]) / 2)
        self.user_list.clear_user_list_cache()
        with WindowCaptureCtx(self.user_list.hwnd):
            scroll_at(x, y, delta)
            time.sleep(CONTACT_SCROLL_WAIT_SECONDS)

    def _scan_context(self) -> tuple:
        region = self.user_list.userList_region
        return (self.user_list.hwnd, tuple(sorted(region.screen_region.items())),
                tuple(sorted(region.image_region.items())), region.full_image.size,
                self.user_list.last_active_name)

    def scan(self, contact_name: str):
        """找不到目标时逐屏向上，再从已确认的顶部向下查找。"""
        started = time.monotonic()
        context = self._scan_context()
        gathered: set[str] = set()
        previous = None
        # QQ 可能将大幅滚轮限制成一次小位移，不能假设一条指令已回到顶部。
        scroll_delta = 120
        pages = 0
        found = None
        try:
            for pages in range(1, CONTACT_SCAN_MAX_PAGES + 1):
                if pages > 1:
                    self.user_list.refresh()
                if self._scan_context() != context:
                    raise RuntimeError("QQ_CONTACT_SCAN_CHANGED")
                if time.monotonic() - started > CONTACT_SCAN_TIMEOUT_SECONDS:
                    raise RuntimeError("QQ_CONTACT_SCAN_LIMIT")
                gathered.update(self.user_list.users)
                found = self.user_list.find_user(contact_name)
                if found is not None:
                    return found
                # 以多个共同名字和行位置确认滚动量，容忍少量 OCR 文本波动。
                positions = {name: user.avatar[1] for name, user in self.user_list.users.items()}
                common = positions.keys() & previous.keys() if previous is not None else set()
                shifts = [previous[name] - positions[name] for name in common]
                unchanged = sum(abs(shift) <= 2 for shift in shifts)
                stationary = bool(positions) and (positions == previous or (
                    unchanged >= 3 and unchanged >= 0.6 * max(len(positions), len(previous or {}))))
                if stationary:
                    if scroll_delta < 0:
                        raise ContactNotFoundError(contact_name)
                    # 已确认顶部，从小步长开始向下；每一屏都检查目标。
                    scroll_delta = -120
                elif len(shifts) >= 3 and abs(median(shifts)) > 2:
                    # 根据真实位移调整下一步，最多六档，并保留至少两行重叠。
                    ys = sorted(positions.values())
                    spacing = median([b - a for a, b in zip(ys, ys[1:])])
                    height = self.user_list.userList_region.image.height
                    step = min(6, max(1, int((height - 2 * spacing) / abs(median(shifts)) * abs(scroll_delta) / 120)))
                    scroll_delta = (120 if scroll_delta > 0 else -120) * step
                previous = positions
                # 未确认真实滚动量前只滚一档，避免跳过边缘联系人。
                if pages < CONTACT_SCAN_MAX_PAGES:
                    self._scroll_list(scroll_delta)
            else:
                raise RuntimeError("QQ_CONTACT_SCAN_LIMIT")
        except Exception as error:
            logger.warning("contacts.scan.failed pages=%d code=%s", pages,
                           type(error).__name__ if isinstance(error, ContactNotFoundError) else str(error))
            raise
        finally:
            self.user_list.clear_user_list_cache()
            logger.info("contacts.scan.complete pages=%d count=%d found=%s duration_ms=%.2f",
                        pages, len(gathered), found is not None, (time.monotonic() - started) * 1000)

