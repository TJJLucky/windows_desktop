"""无障碍树激活行 → 右键菜单模板 → 复制 QQ 号；不执行昵称 OCR 或颜色选行。"""
from __future__ import annotations

import itertools
import logging
import re
import time

import numpy as np
from PIL import Image
import win32api
import win32gui

from ..vision.layout_store import LayoutError
from ..core.clipboard import ClipboardCopy, ClipboardCopyError
from ..core.mouse import click_at, move_to, right_click_at
from ..core.screenshot import WGCCapture, WgcCaptureError
from ..core.window import WindowCaptureCtx
from .accessibility import AccessibilityPane, AccessibilityRow, accessibility_pane
from .contact_lookup import locate, read_pane
from .hotkeys import send_hotkey
from .regions import RegionResult, get_userList_region_and_image
from .template_runtime import find_unique_template, load_image, window_signature
from .window_ops import ensure_qq_session_ready

logger = logging.getLogger("qq_service.contact_number")
MENU_WAIT_SECONDS = 2.0
# 右键菜单总是紧贴被点击的行出现（面板约 205x231）。只在这个小邻域内做匹配，
# 避免对整张好友列表做多尺度扫描（实测整列表 ~43ms，小 ROI 单尺度 ~3ms）。
# 水平两侧都留足一个面板宽度，兼容靠近列表右缘时菜单整体向左翻转。
MENU_ROI_PAD_LEFT = 240
MENU_ROI_PAD_UP = 280
MENU_ROI_PAD_RIGHT = 240
MENU_ROI_PAD_DOWN = 280
# 菜单面板是近白色（>=250）低饱和矩形；好友列表行底色最高只有 245，不会误判。
MENU_PANEL_MIN = 250
MENU_PANEL_MAX_SPREAD = 10
# 实测菜单打开时 ROI 近白占比约 0.24~0.36，未打开时约 0.01，取 0.05 做门槛。
MENU_PANEL_WHITE_FRACTION = 0.05
# 菜单出现后轮询间隔；比旧的 0.1s 更密，让菜单一出现就能被抓到。
MENU_POLL_SECONDS = 0.03
# 面板已出现但始终没有"复制 QQ 号"（例如群聊菜单）时的确认窗口，避免空等满 2s。
MENU_ABSENT_CONFIRM_SECONDS = 0.3
# 菜单截图期间将指针停到列表外的空白处，避免悬停状态影响菜单/窗口画面检查。
# 联系人身份只由 UIA 已选中行及其前后复核决定，不从截图颜色推断。
# 停靠点取窗口右下角的聊天输入区，不落在好友列表行或标题区。
PARK_MARGIN_RIGHT = 60
PARK_MARGIN_BOTTOM = 60
# 相邻两次停靠错开几像素，保证停靠本身一定产生画面变化，随后取到的一定是新帧。
PARK_JITTER = 4
PARK_CONFIRM_SECONDS = 0.2
PARK_SETTLE_SECONDS = 0.05
# 读当前激活联系人时，QQ 偶发重建捕获面（WGC generation 跳变，实测 3->5），
# 单次读取会以 WINDOW_CHANGED 失败；该读取幂等（开菜单->复制->关菜单），
# 因此在有界窗口内重试，别把瞬时重建当成业务失败（真机实测约 1/11 次）。
IDENTIFY_RETRY_ATTEMPTS = 3
IDENTIFY_RETRY_WAIT_SECONDS = 0.25
_park_offsets = itertools.cycle((0, PARK_JITTER))


def _selected_contact_row(pane: AccessibilityPane, *, require_visible: bool = True,
                          require_unique_name: bool = True) -> AccessibilityRow:
    """Return the selected row; name uniqueness is optional for qidian verification."""
    selected = pane.selected_rows()
    if not selected:
        logger.warning("contacts.number.active_not_found rows=%d", len(pane.rows))
        raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_NOT_FOUND")
    if len(selected) != 1:
        logger.warning("contacts.number.active_ambiguous selected=%d rows=%d",
                       len(selected), len(pane.rows))
        raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_AMBIGUOUS")
    row = selected[0]
    if not row.name:
        logger.warning("contacts.number.active_ambiguous name=%r", row.name)
        raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_AMBIGUOUS")
    if require_unique_name:
        name_matches = pane.rows_named(row.name)
        if len(name_matches) != 1:
            logger.warning("contacts.number.active_ambiguous name=%r matches=%d",
                           row.name, len(name_matches))
            raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_AMBIGUOUS")
    if require_visible and not row.visible_in(pane.rect):
        logger.warning("contacts.number.active_not_visible name=%r rect=%s pane=%s",
                       row.name, row.rect, pane.rect)
        raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_NOT_FOUND")
    return row


def _same_accessibility_row(expected: AccessibilityRow, actual: AccessibilityRow) -> bool:
    """Compare same-call UIA identity; runtime IDs are only supplemental within this call."""
    same_runtime_id = (not expected.runtime_id or not actual.runtime_id or
                       expected.runtime_id == actual.runtime_id)
    return (actual.selected and expected.name == actual.name and
            expected.rect == actual.rect and same_runtime_id)


def _verify_selected_row(hwnd: int, expected: AccessibilityRow, *,
                         require_unique_name: bool = True) -> None:
    """Fail closed unless a fresh UIA snapshot still selects the exact same row."""
    actual = _selected_contact_row(accessibility_pane(hwnd),
                                   require_unique_name=require_unique_name)
    if not _same_accessibility_row(expected, actual):
        logger.warning("contacts.number.identity_changed part=accessibility_row "
                       "expected=%r expected_rect=%s actual=%r actual_rect=%s",
                       expected.name, expected.rect, actual.name, actual.rect)
        raise RuntimeError("QQ_CONTACT_NUMBER_ROW_CHANGED")


def _row_image_coordinates(
    region: RegionResult, row: AccessibilityRow
) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
    """Map the UIA screen rectangle into the captured list crop for menu ROI checks."""
    screen = region.screen_region
    screen_width = screen["right"] - screen["left"]
    screen_height = screen["bottom"] - screen["top"]
    if screen_width <= 0 or screen_height <= 0:
        raise RuntimeError("QQ_CONTACT_NUMBER_ROW_CHANGED")
    scale_x = region.image.width / screen_width
    scale_y = region.image.height / screen_height
    left, top, right, bottom = row.rect
    crop_rect = (round((left - screen["left"]) * scale_x),
                 round((top - screen["top"]) * scale_y),
                 round((right - screen["left"]) * scale_x),
                 round((bottom - screen["top"]) * scale_y))
    if (crop_rect[0] < 0 or crop_rect[1] < 0 or
            crop_rect[2] > region.image.width or crop_rect[3] > region.image.height or
            crop_rect[0] >= crop_rect[2] or crop_rect[1] >= crop_rect[3]):
        logger.warning("contacts.number.active_outside_capture name=%r row=%s screen=%s image=%s",
                       row.name, row.rect, screen, region.image.size)
        raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_NOT_FOUND")
    center = ((crop_rect[0] + crop_rect[2]) // 2,
              (crop_rect[1] + crop_rect[3]) // 2)
    return crop_rect, center


def _menu_roi(rect: dict, x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
    """以被点击头像为中心，返回菜单可能出现的整窗小 ROI。

    四边都夹在好友列表区域内，既含面板翻转余量，又不会把左侧导航栏或右侧
    聊天面板（近白）算进来，避免预筛被背景噪声污染。
    """
    cx, cy = rect["left"] + x, rect["top"] + y
    left = max(rect["left"], cx - MENU_ROI_PAD_LEFT)
    top = max(rect["top"], cy - MENU_ROI_PAD_UP)
    right = min(rect["right"], width, cx + MENU_ROI_PAD_RIGHT)
    bottom = min(rect["bottom"], height, cy + MENU_ROI_PAD_DOWN)
    return left, top, max(left + 1, right), max(top + 1, bottom)


def _has_white_panel(crop: Image.Image) -> bool:
    """近白低饱和像素占比预筛；无面板时直接返回，省掉整段模板匹配。"""
    data = np.asarray(crop, dtype=np.int16)
    if data.ndim != 3 or data.size == 0:
        return False
    near_white = ((data.min(axis=2) >= MENU_PANEL_MIN) &
                  ((data.max(axis=2) - data.min(axis=2)) <= MENU_PANEL_MAX_SPREAD))
    return float(near_white.mean()) >= MENU_PANEL_WHITE_FRACTION


def park_pointer(window) -> None:
    """把指针停到窗口右下角的聊天输入区，等画面停在停靠后的状态。

    停靠点不落在好友列表行或标题区；相邻两次停靠错开少量像素，确保后续截图是新帧。
    """
    left, top, right, bottom = win32gui.GetWindowRect(window._hWnd)
    x = min(max(right - PARK_MARGIN_RIGHT, 1), 32767)
    y = min(max(bottom - PARK_MARGIN_BOTTOM - next(_park_offsets), 1), 32767)
    move_to(x, y)
    deadline = time.monotonic() + PARK_CONFIRM_SECONDS
    while win32api.GetCursorPos() != (x, y) and time.monotonic() < deadline:
        time.sleep(0.005)
    if win32api.GetCursorPos() != (x, y):
        logger.warning("contacts.number.park_unconfirmed target=%s actual=%s", (x, y), win32api.GetCursorPos())
    # 先让停靠前的旧帧全部到达，之后的任何一帧都只可能是停靠后的画面。
    time.sleep(PARK_SETTLE_SECONDS)


class ContactNumberReader:
    def capture_region(self, window) -> RegionResult:
        """先把指针停离好友行，再截图并识别好友列表区域。

        返回的基准帧用于判定「行身份有没有变」，因此帧里不能有压在行上的鼠标指针。
        """
        park_pointer(window)
        image = self._capture(window)
        return get_userList_region_and_image(window, image)

    def identify_current(self, *, attempts: int = IDENTIFY_RETRY_ATTEMPTS,
                         deadline: float | None = None, expected_hwnd: int | None = None,
                         require_unique_name: bool = True) -> str | None:
        """复制当前激活行的 QQ 号；窗口捕获面被重建（WINDOW_CHANGED）时整段重试。

        读取本身无副作用且幂等，重试只会重新截取一帧再复制一次，因此只对瞬时
        重建重试；联系人未激活/模板缺失等业务错误直接抛出，不重试。
        """
        for attempt in range(1, attempts + 1):
            self._generation = None
            window = ensure_qq_session_ready(deadline=deadline)
            if expected_hwnd is not None and window._hWnd != expected_hwnd:
                raise RuntimeError("QQ_CONTACT_NUMBER_WINDOW_CHANGED")
            try:
                with WindowCaptureCtx(window._hWnd):
                    pane = read_pane(window._hWnd)
                    selected = _selected_contact_row(pane, require_visible=False,
                                                     require_unique_name=require_unique_name)
                    if not selected.visible_in(pane.rect):
                        # Scroll only: never activate a name inferred from an old tree.
                        # Re-read selection after scrolling before copying any number.
                        reveal_deadline = min(deadline, time.monotonic() + 3.0) if deadline is not None else time.monotonic() + 3.0
                        locate(selected.name, hwnd=window._hWnd, deadline=reveal_deadline)
                        pane = read_pane(window._hWnd)
                        current = _selected_contact_row(pane, require_unique_name=require_unique_name)
                        if (current.name != selected.name or
                                (selected.runtime_id and current.runtime_id and
                                 selected.runtime_id != current.runtime_id)):
                            raise RuntimeError("QQ_CONTACT_NUMBER_ROW_CHANGED")
                        logger.info("contacts.number.active_revealed name=%r", current.name)
                    row = _selected_contact_row(pane, require_unique_name=require_unique_name)
                    region = self.capture_region(window)
                    _row_image_coordinates(region, row)
                    return self.copy_row(window, region, row, require_unique_name=require_unique_name)
            except RuntimeError as error:
                if (str(error) != "QQ_CONTACT_NUMBER_WINDOW_CHANGED"
                        or attempt == attempts):
                    raise
                logger.warning("contacts.identify.retry attempt=%d reason=%s", attempt, error)
                time.sleep(IDENTIFY_RETRY_WAIT_SECONDS)

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

    def copy_row(self, window, region: RegionResult, row: AccessibilityRow, *,
                 require_active: bool = True, require_unique_name: bool = True) -> str | None:
        """Copy QQ number from the row selected by UIA; screenshots never choose a contact."""
        started = time.monotonic()
        signature = window_signature(window)
        rect = region.image_region
        full = region.full_image
        if full is None:
            raise RuntimeError("QQ_CONTACT_NUMBER_WINDOW_CHANGED")
        _, (x, y) = _row_image_coordinates(region, row)
        click_x, click_y = row.center
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
            return frame

        def menu_action(frame):
            """Return menu presence and the Copy QQ Number template match, if any."""
            roi = _menu_roi(rect, x, y, frame.width, frame.height)
            local = frame.crop(roi)
            if not _has_white_panel(local):
                return False, None
            origin = (roi[0], roi[1])
            match = find_unique_template(local, template, threshold=0.90,
                                         origin=origin, multiscale=False)
            if match is None:
                match = find_unique_template(local, template, threshold=0.90, origin=origin)
            if match is None:
                return True, None
            action = {**match, **{key: match[key] + origin[0 if key in ("left", "right") else 1]
                                 for key in ("left", "top", "right", "bottom")}}
            return True, action

        try:
            with WindowCaptureCtx(window._hWnd):
                try:
                    template = load_image("copy_qq_number.png")
                except (LayoutError, OSError) as exc:
                    raise RuntimeError("QQ_CONTACT_NUMBER_TEMPLATE_UNAVAILABLE") from exc
                # Move the pointer away from the contact list before taking fresh evidence.
                park_pointer(window)
                checked_frame()
                if require_active:
                    _verify_selected_row(window._hWnd, row, require_unique_name=require_unique_name)
                menu_open = True
                right_click_at(click_x, click_y, restore=False, smooth=False)
                action_barrier = self._clock()
                require_foreground = True
                deadline = time.monotonic() + MENU_WAIT_SECONDS
                absent_deadline = None
                action = None
                while time.monotonic() < deadline:
                    panel, action = menu_action(checked_frame())
                    if action is not None:
                        break
                    if panel:
                        if absent_deadline is None:
                            absent_deadline = time.monotonic() + MENU_ABSENT_CONFIRM_SECONDS
                        elif time.monotonic() >= absent_deadline:
                            break
                    time.sleep(MENU_POLL_SECONDS)
                if action is None:
                    code = "SKIPPED_COPY_UNAVAILABLE"
                    return None
                # Recheck both the selected UIA row and the live menu before copying anything.
                if require_active:
                    _verify_selected_row(window._hWnd, row, require_unique_name=require_unique_name)
                current = menu_action(checked_frame())[1]
                if current is None or any(abs(current[key] - action[key]) > 2 for key in ("left", "top", "right", "bottom")):
                    raise RuntimeError("QQ_CONTACT_NUMBER_MENU_CHANGED")
                clipboard = ClipboardCopy()
                clipboard.mark()
                click_at(signature["left"] + current["left"] + (current["right"] - current["left"]) // 2,
                         signature["top"] + current["top"] + (current["bottom"] - current["top"]) // 2,
                         restore=False, smooth=False)
                action_barrier = self._clock()
                value = clipboard.read(expected_pid=signature["pid"]).strip()
                if not re.fullmatch(r"[1-9][0-9]{4,19}", value):
                    raise RuntimeError("QQ_CONTACT_NUMBER_INVALID")
                # Closing the menu and moving the pointer do not infer identity from row color.
                park_pointer(window)
                action_barrier = self._clock()
                frame = checked_frame()
                if menu_action(frame)[1] is not None:
                    raise RuntimeError("QQ_CONTACT_NUMBER_MENU_CHANGED")
                menu_open = False
                if require_active:
                    _verify_selected_row(window._hWnd, row, require_unique_name=require_unique_name)
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
