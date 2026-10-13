"""头像行 → 右键菜单模板 → 复制 QQ 号；不执行昵称 OCR 或识别回退。"""
from __future__ import annotations

from dataclasses import dataclass
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
from .hotkeys import send_hotkey
from .regions import RegionResult, get_userList_region_and_image
from .template_runtime import find_unique_template, load_image, window_signature
from .user_list import UserList
from .window_ops import ensure_qq_window_with_retry

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
# 鼠标指针会被 WGC 一起采进画面：指针压在头像上会改变头像像素，让「该行未变」
# 的核对直接误判（实测同一行头像掩膜内均差 8~12，远高于 2.0 门槛）。所以任何要
# 用来做身份比对的截图之前，都必须先把指针停到不覆盖好友行的空白处。
# 停靠点取窗口右下角的聊天输入区：既不在好友列表行上，也不在列表右上标题区里。
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
    """返回当前列表中落在"已激活会话"绝对色带内的那一行。

    真机实测：未激活行 245 灰、被鼠标悬停的未激活行 235 灰、激活行 226 灰（深色主题
    对应更低一档）。悬停在激活行上不会改变它的底色，因此色带判定与指针位置无关，也
    不需要"相对离群"兜底——那种兜底会把常驻 235 灰的未读行也算成候选，在激活行滚出
    可视区时报出假歧义。色带内唯一命中即采纳，多行判歧义，无命中即当前无可见激活会话。
    """
    rows = [ContactRow(avatar, rect) for avatar, rect in UserList.crop_user_row_by_avatar(region.image)]
    if not rows:
        raise RuntimeError("QQ_CONTACT_NUMBER_ACTIVE_NOT_FOUND")
    absolute = [index for index, row in enumerate(rows)
                if UserList.is_active_bg(_row_image(region, row))]
    if len(absolute) == 1:
        return rows[absolute[0]]
    if len(absolute) > 1:
        # 歧义基本只有一个来源：某一行正被鼠标悬停。色带里本该只有一个真激活行，但
        # 常驻 235 灰的行（实测「我的手机」）被悬停时会画成 226 灰，落进同一色带。
        # 打印候选行的纵坐标与底色，让日志能直接指出是哪几行，而不是只留一个错误码。
        logger.warning("contacts.number.active_ambiguous candidates=%s",
                       [(rows[index].avatar[1], UserList.modal_color(_row_image(region, rows[index])))
                        for index in absolute])
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


def visible_rows(region: RegionResult) -> list[ContactRow]:
    """Enumerate avatar-backed rows in the current captured list, top to bottom."""
    rows = [ContactRow(avatar, rect) for avatar, rect in UserList.crop_user_row_by_avatar(region.image)]
    return sorted(rows, key=lambda row: row.avatar[1])


def _same_row_geometry(first: ContactRow, second: ContactRow) -> bool:
    return (abs(first.avatar[0] - second.avatar[0]) <= 2 and
            abs(first.avatar[1] - second.avatar[1]) <= 2 and
            abs(first.avatar[2] - second.avatar[2]) <= 2 and
            all(abs(a - b) <= 2 for a, b in zip(first.rect, second.rect)))


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

    停靠点既不落在好友列表行上，也不落在列表右上角的会话标题区，因此不会进入任何
    身份比对区域；相邻两次停靠错开 PARK_JITTER 像素，保证后续读取拿到的是新帧。
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

    def identify_current(self, *, attempts: int = IDENTIFY_RETRY_ATTEMPTS) -> str | None:
        """复制当前激活行的 QQ 号；窗口捕获面被重建（WINDOW_CHANGED）时整段重试。

        读取本身无副作用且幂等，重试只会重新截取一帧再复制一次，因此只对瞬时
        重建重试；联系人未激活/模板缺失等业务错误直接抛出，不重试。
        """
        for attempt in range(1, attempts + 1):
            self._generation = None
            window = ensure_qq_window_with_retry()
            try:
                with WindowCaptureCtx(window._hWnd):
                    region = self.capture_region(window)
                    return self.copy_row(window, region, _active_row(region))
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

    def copy_row(self, window, region: RegionResult, row: ContactRow, *, require_active: bool = True) -> str | None:
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
            # 标题区（列表右上方的活跃会话标题/聊天区）会因对方发来消息或界面动画
            # 随时重绘（真机实测偶发），与"这一行还是不是同一行"无关；枚举阶段（
            # require_active=False）跳过该比对，行身份由窗口签名、头像掩膜与关闭菜单后
            # 的几何核对保证。激活链路仍保留标题区核对以做最强一致性确认。
            if require_active and not _same_image(header_image, frame.crop(header_box)):
                logger.warning("contacts.number.identity_changed part=header")
                raise RuntimeError("QQ_CONTACT_NUMBER_ROW_CHANGED")
            return frame

        def menu_action(frame):
            """返回 (面板是否出现, "复制 QQ 号"命中坐标或 None)。

            只匹配点击点附近的小 ROI，并先用近白面板占比做廉价预筛。
            """
            roi = _menu_roi(rect, x, y, frame.width, frame.height)
            local = frame.crop(roi)
            if not _has_white_panel(local):
                return False, None
            origin = (roi[0], roi[1])
            # 菜单与模板同尺寸：先固定尺度快路径，未命中再回退多尺度。
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
                # 上一次操作可能把指针留在了本行头像上（例如上一行的菜单项位置），
                # 先停离列表，保证下面这次基准核对的两帧都不含指针。
                park_pointer(window)
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
                absent_deadline = None
                action = None
                while time.monotonic() < deadline:
                    panel, action = menu_action(checked_frame())
                    if action is not None:
                        break
                    if panel:
                        # 面板已出现但看不到"复制 QQ 号"（例如群聊菜单是"复制群号"）：
                        # 只需极短确认窗口即可判定不可用，不再空等满 MENU_WAIT_SECONDS。
                        if absent_deadline is None:
                            absent_deadline = time.monotonic() + MENU_ABSENT_CONFIRM_SECONDS
                        elif time.monotonic() >= absent_deadline:
                            break
                    time.sleep(MENU_POLL_SECONDS)
                if action is None:
                    code = "SKIPPED_COPY_UNAVAILABLE"
                    return None
                # 点击前用新截图确认菜单仍在同一位置，不点击缓存菜单坐标。
                current = menu_action(checked_frame())[1]
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
                # 点击菜单项后指针就停在菜单项上，而菜单紧贴被点的行出现：指针可能正压着
                # 相邻的一行。真机实测常驻 235 灰的行（如「我的手机」）被悬停时画成 226 灰，
                # 与激活行同档，于是下面的激活行复核会同时命中两行，误报 ACTIVE_AMBIGUOUS。
                # 复制动作已经结束，这里先把指针停回列表外的空白处再取复核帧，让复核帧与
                # 开头基准帧保持同一不变式：没有悬停高亮、也没有压在行上的指针。
                park_pointer(window)
                action_barrier = self._clock()
                frame = checked_frame()
                if menu_action(frame)[1] is not None:
                    raise RuntimeError("QQ_CONTACT_NUMBER_MENU_CHANGED")
                menu_open = False
                # 菜单关闭后，原头像和文字区必须仍属于同一行。扫描非激活行时不能要求它
                # 已经是激活行；激活/发送链路仍用 require_active=True 做严格复核。
                local = frame.crop((rect["left"], rect["top"], rect["right"], rect["bottom"]))
                after = RegionResult(region.screen_region, region.image_region, local, frame)
                same_geometry = any(_same_row_geometry(row, candidate) for candidate in visible_rows(after))
                if not same_geometry or not _same_avatar(avatar_image, frame.crop(avatar_box)):
                    logger.warning("contacts.number.identity_changed part=closed_row")
                    raise RuntimeError("QQ_CONTACT_NUMBER_ROW_CHANGED")
                if require_active and not _same_row_geometry(row, _active_row(after)):
                    logger.warning("contacts.number.identity_changed part=active_row")
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
