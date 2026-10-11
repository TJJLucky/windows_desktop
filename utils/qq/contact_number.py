"""头像行 → 右键菜单模板 → 复制 QQ 号；不执行昵称 OCR 或识别回退。"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import time

import numpy as np
from PIL import Image
import win32gui

from ..vision.layout_store import LayoutError
from ..core.clipboard import ClipboardCopy, ClipboardCopyError
from ..core.mouse import click_at, right_click_at
from ..core.screenshot import WGCCapture, WgcCaptureError
from ..core.window import WindowCaptureCtx
from .hotkeys import send_hotkey
from .regions import RegionResult, get_userList_region_and_image
from .template_runtime import find_unique_template, load_image, window_signature
from .user_list import UserList
from .window_ops import ensure_qq_window_with_retry

logger = logging.getLogger("qq_service.contact_number")
MENU_WAIT_SECONDS = 2.0


@dataclass(frozen=True)
class ContactRow:
    """本次截图中的内部行坐标；不接受来自 HTTP 的坐标。"""

    avatar: tuple[int, int, int]
    rect: tuple[int, int, int, int]


def _row_image(region: RegionResult, row: ContactRow) -> Image.Image:
    x, _, radius = row.avatar
    _, top, right, bottom = row.rect
    return region.image.crop((x + radius + 5, top, right, bottom))


def _active_row(region: RegionResult) -> ContactRow:
    rows = [ContactRow(avatar, rect) for avatar, rect in UserList.crop_user_row_by_avatar(region.image)]
    images = [_row_image(region, row) for row in rows]
    if not images:
        raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_NOT_FOUND")
    # 绝对色带（浅色 226 / 深色 113 一档）是主判据；相对离群（主题无关）只做否决与兜底。
    # 实测同一列表里激活行(226)与鼠标悬停行(235)都是离群色，因此离群集合出现多个候选时
    # 仍需先看色带结论：只要色带命中行也在离群集合内，就采纳色带结果，绝不因悬停多一个候选放弃。
    absolute = [index for index, image in enumerate(images) if UserList.is_active_bg(image)]
    relative = UserList.active_row_index([UserList.modal_color(image) for image in images])
    if len(absolute) > 1:
        raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_AMBIGUOUS")
    if len(absolute) == 1:
        if relative and absolute[0] not in relative:
            raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_AMBIGUOUS")
        return rows[absolute[0]]
    if len(relative) == 1:
        return rows[relative[0]]
    if len(relative) > 1:
        raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_AMBIGUOUS")
    raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_NOT_FOUND")


def _same_image(first: Image.Image, second: Image.Image) -> bool:
    if first.size != second.size:
        return False
    return float(np.abs(np.asarray(first.convert("RGB"), dtype=np.int16) -
                        np.asarray(second.convert("RGB"), dtype=np.int16)).mean()) <= 2.0


def _same_avatar(first: Image.Image, second: Image.Image) -> bool:
    if first.size != second.size:
        return False
    width, height = first.size
    yy, xx = np.ogrid[:height, :width]
    # 圆外是行背景；菜单激活会改变背景，不属于头像身份。
    mask = (xx - width / 2) ** 2 + (yy - height / 2) ** 2 <= (min(width, height) * 0.45) ** 2
    difference = np.abs(np.asarray(first.convert("RGB"), dtype=np.int16) -
                        np.asarray(second.convert("RGB"), dtype=np.int16))
    return float(difference[mask].mean()) <= 2.0


class ContactNumberReader:
    def identify_current(self) -> str | None:
        self._generation = None
        window = ensure_qq_window_with_retry()
        with WindowCaptureCtx(window._hWnd):
            image = self._capture(window)
            region = get_userList_region_and_image(window, image)
            return self.copy_row(window, region, _active_row(region))

    @staticmethod
    def _clock():
        return WGCCapture().clock_100ns()

    def _capture(self, window, *, after_100ns=None):
        try:
            frame = WGCCapture().capture_frame(window._hWnd, after_100ns=after_100ns)
        except WgcCaptureError as exc:
            raise RuntimeError("QQ_CONTACT_NUMBER_" + exc.code) from exc
        previous = getattr(self, "_generation", None)
        if previous is not None and previous != frame.info.generation:
            raise RuntimeError("QQ_CONTACT_NUMBER_WINDOW_CHANGED")
        self._generation = frame.info.generation
        return frame.image

    def copy_row(self, window, region: RegionResult, row: ContactRow) -> str | None:
        """复制内部已识别的行；当前阶段只由 identify_current 调用。"""
        started = time.monotonic()
        signature = window_signature(window)
        rect = region.image_region
        full = region.full_image
        x, y, radius = row.avatar
        avatar_box = (rect["left"] + x - radius, rect["top"] + y - radius,
                      rect["left"] + x + radius, rect["top"] + y + radius)
        header_box = (rect["right"], max(0, rect["top"] - 60), full.width, rect["top"])
        # 左侧菜单会遮住头像，菜单期间核对标题，关闭后再核对完整行和头像。
        avatar_image, header_image = full.crop(avatar_box), full.crop(header_box)
        menu_open = False
        require_foreground = False
        code = "SUCCESS"
        action_barrier = None

        def checked_frame():
            if window_signature(window) != signature or not win32gui.IsWindowVisible(window._hWnd) or win32gui.IsIconic(window._hWnd):
                raise RuntimeError("QQ_CONTACT_NUMBER_WINDOW_CHANGED")
            if require_foreground and win32gui.GetForegroundWindow() != window._hWnd:
                foreground = win32gui.GetForegroundWindow()
                logger.warning("contacts.number.foreground_changed expected=%s actual=%s title=%r",
                               window._hWnd, foreground, win32gui.GetWindowText(foreground))
                raise RuntimeError("QQ_CONTACT_NUMBER_WINDOW_CHANGED")
            frame = self._capture(window, after_100ns=action_barrier)
            if window_signature(window) != signature:
                raise RuntimeError("QQ_CONTACT_NUMBER_WINDOW_CHANGED")
            if frame.size != full.size:
                logger.warning("contacts.number.identity_changed part=frame_size")
                raise RuntimeError("QQ_CONTACT_NUMBER_ROW_CHANGED")
            if not menu_open and not _same_avatar(avatar_image, frame.crop(avatar_box)):
                logger.warning("contacts.number.identity_changed part=avatar")
                raise RuntimeError("QQ_CONTACT_NUMBER_ROW_CHANGED")
            if not _same_image(header_image, frame.crop(header_box)):
                logger.warning("contacts.number.identity_changed part=header")
                raise RuntimeError("QQ_CONTACT_NUMBER_ROW_CHANGED")
            return frame

        def menu_action(frame):
            local = frame.crop((rect["left"], rect["top"], rect["right"], rect["bottom"]))
            match = find_unique_template(local, template, threshold=0.90,
                                         origin=(rect["left"], rect["top"]))
            if match is None:
                return None
            return {**match, **{key: match[key] + rect["left" if key in ("left", "right") else "top"]
                               for key in ("left", "top", "right", "bottom")}}

        try:
            with WindowCaptureCtx(window._hWnd):
                try:
                    template = load_image("copy_qq_number.png")
                except (LayoutError, OSError) as exc:
                    raise RuntimeError("QQ_CONTACT_NUMBER_TEMPLATE_UNAVAILABLE") from exc
                checked_frame()
                click_x = region.screen_region["left"] + x
                click_y = region.screen_region["top"] + y
                if (radius <= 0 or not (0 <= x < region.image.width) or
                        not (0 <= row.rect[1] < y < row.rect[3] <= region.image.height)):
                    raise RuntimeError("QQ_CONTACT_NUMBER_ROW_CHANGED")
                menu_open = True
                right_click_at(click_x, click_y, restore=False, smooth=False)
                action_barrier = self._clock()
                require_foreground = True
                deadline = time.monotonic() + MENU_WAIT_SECONDS
                action = None
                while time.monotonic() < deadline:
                    frame = checked_frame()
                    action = menu_action(frame)
                    if action is not None:
                        break
                    time.sleep(0.1)
                if action is None:
                    code = "SKIPPED_COPY_UNAVAILABLE"
                    return None
                # 点击前用新截图确认菜单仍在同一位置，不点击缓存菜单坐标。
                current = menu_action(checked_frame())
                if current is None or any(abs(current[key] - action[key]) > 2 for key in ("left", "top", "right", "bottom")):
                    raise RuntimeError("QQ_CONTACT_NUMBER_MENU_CHANGED")
                clipboard = ClipboardCopy()
                clipboard.mark()
                click_at(signature["left"] + current["left"] + (current["right"] - current["left"]) // 2,
                         signature["top"] + current["top"] + (current["bottom"] - current["top"]) // 2, restore=False, smooth=False)
                action_barrier = self._clock()
                value = clipboard.read(expected_pid=signature["pid"]).strip()
                if not re.fullmatch(r"[1-9][0-9]{4,19}", value):
                    raise RuntimeError("QQ_CONTACT_NUMBER_INVALID")
                frame = checked_frame()
                if menu_action(frame) is not None:
                    raise RuntimeError("QQ_CONTACT_NUMBER_MENU_CHANGED")
                menu_open = False
                # 原位置必须仍为激活行，且菜单关闭后的文字区也必须属于同一行。
                local = frame.crop((rect["left"], rect["top"], rect["right"], rect["bottom"]))
                after = RegionResult(region.screen_region, region.image_region, local, frame)
                if (not _same_avatar(avatar_image, frame.crop(avatar_box)) or _active_row(after) != row or
                        not _same_image(_row_image(region, row), _row_image(after, row))):
                    logger.warning("contacts.number.identity_changed part=closed_row")
                    raise RuntimeError("QQ_CONTACT_NUMBER_ROW_CHANGED")
                return value
        except ClipboardCopyError as exc:
            code = f"QQ_CONTACT_NUMBER_{exc}"
            raise RuntimeError(code) from exc
        except Exception as exc:
            code = str(exc) if str(exc).startswith("QQ_") else type(exc).__name__
            raise
        finally:
            # 仅在 QQ 仍是前台时关闭菜单，避免把 Esc 发给用户切换到的应用。
            if menu_open and win32gui.GetForegroundWindow() == window._hWnd:
                send_hotkey(("esc",))
            logger.info("contacts.number.copy code=%s duration_ms=%.2f", code,
                        (time.monotonic() - started) * 1000)
