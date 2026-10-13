"""QQ 会话列表按昵称定位、滚动查找与激活（无障碍树）。

身份口径是会话行在无障碍树里自报的名字（好友昵称/备注、群名、官方号名），
不再使用 QQ 号，也不做昵称 OCR。整条链路只依赖 UI Automation：

1. ``accessibility.pane`` 枚举列表容器，一次拿到"视口附近一屏半"的行，行自带
   屏幕物理像素矩形，跨电脑 DPI 不需要再换算；
2. 目标不可见时滚动查找：优先用列表容器的 UIA ScrollPattern 直接定位滚动百分比
   （真机实测 SetScrollPercent 约 2~9ms、重读一屏行约 40~65ms），容器不发布该
   Pattern 时退回鼠标滚轮逐段下滚；
3. 命中行的中心点就是点击坐标；点击到 QQ 画出新会话之间有一段重绘延迟，确认环节
   在 ACTIVATE_CONFIRM_TIMEOUT_SECONDS 内轮询聊天区标题（失败再用激活行标记复核）；
4. 名单里有多个同名会话时抛 ContactAmbiguousError，不做猜测。
5. 名单里筛掉 QQ 没给复制项的服务/系统会话（真机实测："我的手机""QQ邮箱提醒"，
   判据见 accessibility._row_copyable），以及本机读过的"QQ 拒绝复制"会话
   （真机实测："QQ团队"、临时会话的邀请卡片、只含系统占位消息的会话，见 record_refused_copy）；
   它们进不了可用名单，对它们发起消息读取时直接抛 ContactNotCopyableError，
   不再跑一遍十几秒的框选失败路径。

滚动扫掠的步长按"树里已渲染行的跨度 × SWEEP_SAFETY_RATIO"推出，因此相邻两段必然
重叠、不会漏行；列表不足一屏（ScrollPattern 报不可滚动）时根本不进入扫掠。
"""

from __future__ import annotations

# json：读写被 QQ 拒绝复制的会话学习记录
import json
# logging：记录查找耗时与失败原因
import logging
# os：读取学习开关与 state 目录环境变量
import os
# time：滚动稳定轮询与激活确认的重试间隔
import time
# Path：state 目录下的记录文件路径
from pathlib import Path

# 无障碍树读取（行/列表区/聊天区标题）
from .accessibility import (
    AccessibilityPane,
    AccessibilityRow,
    AccessibilityUnavailableError,
    accessibility_chat,
    accessibility_pane,
    accessibility_scroll_state,
    accessibility_set_scroll_percent,
)
# 点击与滚轮原语
from ..core.mouse import click_at, scroll_at
# 无焦点置顶上下文（点击/滚轮前保证窗口在屏内可见）
from ..core.window import WindowCaptureCtx
# 窗口就绪保证
from .window_ops import ensure_qq_window_with_retry


logger = logging.getLogger("qq_service.contact_lookup")

# 昵称长度上限（与 HTTP 契约一致）
CONTACT_NAME_MAX_LENGTH = 256

# 滚动后等待元素树稳定的轮询：重绘期间树会瞬时返回空或旧内容，连续两次读到同一份
# 名单才认为稳定
SETTLE_TIMEOUT_SECONDS = 1.5
SETTLE_INTERVAL_SECONDS = 0.06

# 扫掠步长 = 已渲染行跨度 × 该比例；上下限避免退化
SWEEP_SAFETY_RATIO = 0.8
SWEEP_MIN_STEP_PERCENT = 5.0
SWEEP_MAX_STEP_PERCENT = 50.0
SWEEP_MAX_STEPS = 40

# 滚轮回退：一次注入的滚轮量与最多注入次数（QQ 的滚轮滚动是带动画的，实测每次
# 注入位移不稳定，因此回退路径按"名单不再变化"判定到底，而不是估算步长）
WHEEL_DELTA = -1200
WHEEL_MAX_STEPS = 120

# 点击后等待会话切换生效的上限与轮询间隔
ACTIVATE_CONFIRM_TIMEOUT_SECONDS = 4.0
ACTIVATE_CONFIRM_INTERVAL_SECONDS = 0.2


# ── 业务错误 ────────────────────────────────────────────────────────
class ContactLookupError(RuntimeError):
    """按昵称定位会话时的可公开业务错误基类（消息以稳定错误码开头）。"""


class ContactNameInvalidError(ContactLookupError):
    """昵称为空或超长。"""

    def __init__(self) -> None:
        super().__init__("QQ_CONTACT_NAME_INVALID: 联系人昵称不能为空且不超过 256 个字符")


class ContactNotFoundError(ContactLookupError):
    """滚动查找后仍没有该昵称的会话。"""

    def __init__(self, contact_name: str) -> None:
        self.contact_name = contact_name
        super().__init__(f"QQ_CONTACT_NOT_FOUND: 会话列表滚动查找后未找到昵称「{contact_name}」")


class ContactAmbiguousError(ContactLookupError):
    """名单里有多个同名会话，无法确定目标。"""

    def __init__(self, contact_name: str, count: int) -> None:
        self.contact_name = contact_name
        super().__init__(f"QQ_CONTACT_AMBIGUOUS: 会话列表里有 {count} 个同名会话「{contact_name}」")


class ContactActivationError(ContactLookupError):
    """点击后当前会话仍不是目标昵称。"""

    def __init__(self, contact_name: str, actual: str) -> None:
        self.contact_name = contact_name
        super().__init__(
            f"QQ_CONTACT_ACTIVATION_FAILED: 点击「{contact_name}」后当前会话是「{actual or '未知'}」")


class ContactNotCopyableError(ContactLookupError):
    """目标会话被 QQ 禁用了消息复制（服务/系统会话，如"我的手机""QQ邮箱提醒"）。"""

    def __init__(self, contact_name: str) -> None:
        self.contact_name = contact_name
        super().__init__(
            f"QQ_CONTACT_NOT_COPYABLE: 会话「{contact_name}」不支持框选复制消息"
            "（QQ 没给它复制项），请改用别的会话")


# ── QQ 拒绝复制的会话（本机学习记录）────────────────────────────────
# 行结构判据（accessibility._row_copyable）能挡住"我的手机""QQ邮箱提醒"这类行内挂着
# <img> 的服务/系统会话；真机上还有一批行结构与普通会话一样、QQ 却同样不给复制的会话：
# 官方号"QQ团队"（框选始终进不了多选态，真机实测白等 13.7s 后报 QQ_SELECTION_NOT_ENTERED）、
# 临时会话里的邀请卡片（进了多选态但剪贴板不变）、只含"暂不支持该消息类型"占位消息的会话。
# 把它们留在名单里，每次读取都要重跑一遍失败的框选（还是在真机上把服务拖进 COM 崩溃
# 风险最大的那条链路），因此凡是"确实试过、QQ 明确拒绝"的会话按名字记到 state 目录：
# 之后从名单里筛掉，直接读取它们则毫秒级报 QQ_CONTACT_NOT_COPYABLE。
#
# 记录只针对本机这个 QQ 账号，随 state 目录走；可关可删：
#   QQ_CONTACT_REFUSAL_LEARNING=0        关闭学习（名单不再筛）
#   删除 <state>/non-copyable-contacts.json  清空记录
REFUSAL_LEARNING_ENV = "QQ_CONTACT_REFUSAL_LEARNING"
REFUSAL_STORE_FILENAME = "non-copyable-contacts.json"
REFUSAL_MAX_ENTRIES = 200
# 只有这些失败码代表"QQ 拒绝复制"：窗口不可用、截图失败、解析失败等真实错误不记。
REFUSAL_FAILURE_CODES = frozenset({
    "QQ_SELECTION_NOT_ENTERED",
    "QQ_COPY_CLIPBOARD_UNCHANGED",
    "QQ_COPY_TEXT_EMPTY",
    "QQ_COPY_ACTION_NOT_FOUND",
})
# 弱证据：拖拽没能进入框选态。没有消息的空会话（画布是纯色，但像素判空没认出来：
# 右侧资料栏顶掉了消息视口、别的主题/DPI 下底色采样失手、截图取到一帧陈旧画面……）
# 也会报这个码。这种会话必须留在名单里，因此它单独一次失败不生效，连续两次才把它
# 记成"QQ 拒绝复制"——否则一个真实（只是没消息）的会话会被永久筛掉，静默丢掉供应商。
# 进了多选态之后才失败的几个码是强证据（QQ 确实拒绝了这次复制），一次即生效。
REFUSAL_WEAK_CODES = frozenset({"QQ_SELECTION_NOT_ENTERED"})
REFUSAL_MIN_STRIKES = 2


def _refusal_learning_enabled() -> bool:
    """学习开关：默认开；显式设成 0/false/no/off 时关闭。"""
    value = os.environ.get(REFUSAL_LEARNING_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _state_dir() -> Path:
    """服务 state 目录：与消息诊断目录同一套解析规则（env → 默认安装目录）。"""
    cache_db = os.environ.get("QQ_WINDOW_CACHE_DB")
    if cache_db:
        return Path(cache_db).expanduser().resolve().parent
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "price-agent-qq-service" / "state"


def refusal_store_path() -> Path:
    """学习记录文件路径（诊断/清空时引用）。"""
    return _state_dir() / REFUSAL_STORE_FILENAME


def refused_contacts() -> dict[str, dict]:
    """读取学习记录；文件缺失/损坏/开关关闭时返回空表，绝不影响读取链路。"""
    if not _refusal_learning_enabled():
        return {}
    try:
        payload = json.loads(refusal_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    contacts = payload.get("contacts") if isinstance(payload, dict) else None
    return contacts if isinstance(contacts, dict) else {}


def _strikes_required(code: str) -> int:
    """该失败码要连续记几次才生效（弱证据见 ``REFUSAL_WEAK_CODES``）。"""
    return REFUSAL_MIN_STRIKES if code in REFUSAL_WEAK_CODES else 1


def refusal_is_effective(info: dict | None) -> bool:
    """一条学习记录是否已生效（够次数）。"""
    if not isinstance(info, dict):
        return False
    return int(info.get("count") or 0) >= _strikes_required(str(info.get("code") or ""))


def confirmed_refusals() -> dict[str, dict]:
    """已生效的拒绝记录（名单筛除与快速报错都用它）。"""
    return {name: info for name, info in refused_contacts().items()
            if refusal_is_effective(info)}


def is_refused(contact_name: str) -> bool:
    """该昵称是否已被确认为"QQ 拒绝复制"（弱证据要够次数才算）。"""
    return refusal_is_effective(refused_contacts().get(contact_name))


def record_refused_copy(contact_name: str, failure_code: str) -> None:
    """记录一次被 QQ 拒绝复制（含失败码与次数）；记录失败不影响主流程。"""
    name = (contact_name or "").strip()
    code = str(failure_code or "").split(":", 1)[0].strip()
    if not name or code not in REFUSAL_FAILURE_CODES or not _refusal_learning_enabled():
        return
    try:
        path = refusal_store_path()
        contacts = dict(refused_contacts())
        previous = contacts.get(name) if isinstance(contacts.get(name), dict) else {}
        contacts[name] = {
            "code": code,
            "count": int(previous.get("count") or 0) + 1,
            "lastAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if len(contacts) > REFUSAL_MAX_ENTRIES:
            ordered = sorted(contacts.items(), key=lambda item: str(item[1].get("lastAt") or ""))
            contacts = dict(ordered[-REFUSAL_MAX_ENTRIES:])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schemaVersion": 1, "contacts": contacts}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        entry = contacts[name]
        if refusal_is_effective(entry):
            logger.warning("contacts.refused.learned contact=%s code=%s count=%s",
                           name, code, entry["count"])
        else:
            logger.warning(
                "contacts.refused.pending contact=%s code=%s count=%s strikes=%s",
                name, code, entry["count"], _strikes_required(code))
    except OSError as exc:  # 只是学习记录，写不进去也必须让原错误照常抛出
        logger.warning("contacts.refused.record_failed contact=%s error=%s", name, exc)


# ── 名单读取 ────────────────────────────────────────────────────────
def normalize_name(contact_name: str | None) -> str:
    """校验并规范化昵称（去首尾空白）。"""
    name = (contact_name or "").strip()
    if not name or len(name) > CONTACT_NAME_MAX_LENGTH:
        raise ContactNameInvalidError()
    return name


def _pane_signature(pane: AccessibilityPane) -> tuple:
    """名单指纹：行名 + 屏幕矩形；用于判断"元素树是否稳定/列表是否还在动"。"""
    return tuple((row.name, row.rect) for row in pane.rows)


def read_pane(hwnd: int) -> AccessibilityPane:
    """读一屏名单，等到元素树稳定（连续两次读到同一份名单）再返回。"""
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    pane = accessibility_pane(hwnd)
    previous = None
    while True:
        signature = _pane_signature(pane)
        # 元素树重建期间会瞬时返回空名单：继续等到有行或超时为止
        if signature == previous and pane.rows:
            return pane
        if time.monotonic() >= deadline:
            return pane
        previous = signature
        time.sleep(SETTLE_INTERVAL_SECONDS)
        pane = accessibility_pane(hwnd)


def _match(pane: AccessibilityPane, name: str) -> tuple[AccessibilityRow | None, int]:
    """精确匹配昵称，返回 (命中行, 同名行数)；同名多于一行时命中行返回 None。"""
    rows = pane.rows_named(name)
    if not rows:
        return None, 0
    if len(rows) > 1:
        return None, len(rows)
    return rows[0], 1


def _contact_name_of(hwnd: int) -> str:
    """当前会话名（聊天区标题）；读不到时返回空串。"""
    try:
        return accessibility_chat(hwnd).contact_name
    except AccessibilityUnavailableError:
        return ""


def _selected_name(hwnd: int) -> str:
    """当前激活行（ClassName 带 --selected 的行）名字；读不到时返回空串。"""
    try:
        selected = accessibility_pane(hwnd).selected_rows()
    except AccessibilityUnavailableError:
        return ""
    return selected[0].name if len(selected) == 1 else ""


def snapshot(hwnd: int, include_non_copyable: bool = False) -> dict[str, dict]:
    """当前名单快照（昵称 → 行信息）；含树里已渲染但滚出视口的行。

    默认筛掉 QQ 没提供框选复制的服务/系统会话（"我的手机""QQ邮箱提醒"这类）：
    它们读不了消息，留在名单里只会让调用方（和真机测试）走到 4 秒的失败路径。
    ``include_non_copyable=True`` 时全部保留，并用行信息里的 ``copyable=False``
    标出被筛掉的那些（诊断用）。
    """
    pane = read_pane(hwnd)
    refused = confirmed_refusals()
    contacts: dict[str, dict] = {}
    filtered: list[str] = []
    refused_in_view: list[str] = []
    for row in pane.rows:
        if not row.name:
            continue
        if not row.copyable and not include_non_copyable:
            filtered.append(row.name)
            continue
        if row.name in refused and not include_non_copyable:
            refused_in_view.append(row.name)
            continue
        contacts[row.name] = {
            "rect": list(row.rect),
            "texts": list(row.texts),
            "active": row.selected,
            "pinned": row.pinned,
            "copyable": row.copyable,
            "refused": row.name in refused,
            "unread": row.unread,
            "unreadCount": row.unread_count,
        }
    if filtered:
        logger.info("contacts.snapshot filtered_non_copyable names=%s", filtered)
    if refused_in_view:
        logger.info("contacts.snapshot filtered_refused names=%s", refused_in_view)
    return contacts


# ── 滚动查找 ────────────────────────────────────────────────────────
def _sweep_step_percent(state, pane: AccessibilityPane) -> float:
    """扫掠步长：不超过"已渲染行跨度"的 SWEEP_SAFETY_RATIO，保证相邻两段重叠。"""
    viewport = float(max(1, pane.rect[3] - pane.rect[1]))
    content = state.content_height(viewport)
    if not pane.rows:
        return SWEEP_MIN_STEP_PERCENT
    span = float(max(1, pane.rows[-1].rect[3] - pane.rows[0].rect[1]))
    step = SWEEP_SAFETY_RATIO * span / content * 100.0
    return min(SWEEP_MAX_STEP_PERCENT, max(SWEEP_MIN_STEP_PERCENT, step))


def _sweep_by_percent(
        hwnd: int, name: str, pane: AccessibilityPane) -> tuple[AccessibilityRow | None, bool]:
    """用 ScrollPattern 从列表顶部按步长扫到底部，逐段精确匹配。

    返回 ``(命中行, 是否已覆盖全表)``。``covered=True`` 表示百分比扫掠已经把整张
    名单都过了一遍（或名单不足一屏），找不到就是真的没有，调用方不必再跑慢速
    滚轮回退；容器不发布 ScrollPattern 时返回 ``(None, False)``。
    """
    try:
        state = accessibility_scroll_state(hwnd)
    except AccessibilityUnavailableError:
        return None, False
    if not state.scrollable:
        # 名单不足一屏：当前这一屏就是全部
        return None, True
    step = _sweep_step_percent(state, pane)
    percent = 0.0
    for _ in range(SWEEP_MAX_STEPS + 1):
        try:
            accessibility_set_scroll_percent(hwnd, percent)
        except AccessibilityUnavailableError:
            return None, False
        current = read_pane(hwnd)
        row, count = _match(current, name)
        if count > 1:
            raise ContactAmbiguousError(name, count)
        if row is not None and row.visible_in(current.rect):
            return row, True
        if percent >= 100.0:
            return None, True
        percent = min(100.0, percent + step)
    raise ContactLookupError("QQ_CONTACT_SCAN_LIMIT: 会话列表滚动扫掠超过步数上限")


def _wheel_sweep(hwnd: int, name: str, pane: AccessibilityPane, delta: int) -> AccessibilityRow | None:
    """滚轮逐段滚动并逐段匹配，直到名单不再变化（到顶/到底）。

    返回命中行或 None；同时把最后一段名单写回 ``pane`` 的调用方（此处不需要）。
    """
    x = (pane.rect[0] + pane.rect[2]) // 2
    y = (pane.rect[1] + pane.rect[3]) // 2
    previous = _pane_signature(pane)
    for _ in range(WHEEL_MAX_STEPS):
        with WindowCaptureCtx(hwnd):
            scroll_at(x, y, delta)
        current = read_pane(hwnd)
        row, count = _match(current, name)
        if count > 1:
            raise ContactAmbiguousError(name, count)
        if row is not None and row.visible_in(current.rect):
            return row
        signature = _pane_signature(current)
        if signature == previous:
            return None
        previous = signature
    raise ContactLookupError("QQ_CONTACT_SCAN_LIMIT: 滚轮滚动查找超过步数上限")


def _sweep_wheel(hwnd: int, name: str, pane: AccessibilityPane) -> AccessibilityRow | None:
    """滚轮回退路径：先回到列表顶部，再逐段向下查找。"""
    # 目标可能在视口上方：只向下滚永远找不到，先向上滚到不再移动为止
    up = _wheel_sweep(hwnd, name, pane, -WHEEL_DELTA)
    if up is not None:
        return up
    current = read_pane(hwnd)
    return _wheel_sweep(hwnd, name, current, WHEEL_DELTA)


def _reveal(hwnd: int, row: AccessibilityRow, pane: AccessibilityPane) -> AccessibilityRow | None:
    """把"已渲染但被视口边缘裁掉"的行推进视口（按内容高度换算滚动百分比）。"""
    try:
        state = accessibility_scroll_state(hwnd)
    except AccessibilityUnavailableError:
        return None
    viewport = float(max(1, pane.rect[3] - pane.rect[1]))
    content = state.content_height(viewport)
    # 正数表示目标在视口中心下方，需要向下滚
    shift = row.center[1] - (pane.rect[1] + pane.rect[3]) // 2
    percent = state.position_percent + shift / content * 100.0
    if not 0.0 <= percent <= 100.0:
        return None
    try:
        accessibility_set_scroll_percent(hwnd, percent)
        current = read_pane(hwnd)
    except AccessibilityUnavailableError:
        return None
    candidate, count = _match(current, row.name)
    if count == 1 and candidate is not None and candidate.visible_in(current.rect):
        return candidate
    return None


def locate(contact_name: str, hwnd: int | None = None) -> AccessibilityRow:
    """按昵称精确定位一个"完整可见、可安全点击"的会话行。

    目标不在当前视口时滚动查找；没找到抛 ContactNotFoundError，同名多行抛
    ContactAmbiguousError，无障碍树不可用抛 AccessibilityUnavailableError。
    """
    name = normalize_name(contact_name)
    hwnd = hwnd or ensure_qq_window_with_retry()._hWnd
    started = time.monotonic()
    pane = read_pane(hwnd)
    home_percent = _scroll_percent(hwnd)
    row, count = _match(pane, name)
    if count > 1:
        raise ContactAmbiguousError(name, count)
    if row is not None and row.visible_in(pane.rect):
        return row
    # 已渲染但被视口裁掉：先按内容高度把它推进视口，省掉整轮扫掠
    if row is not None:
        revealed = _reveal(hwnd, row, pane)
        if revealed is not None:
            logger.info("contacts.locate contact=%s mode=reveal duration_ms=%.1f",
                        name, (time.monotonic() - started) * 1000)
            return revealed
    # 整轮扫掠；失败时把列表滚回原来的位置，别把用户的名单留在别处
    found, covered = _sweep_by_percent(hwnd, name, pane)
    # 百分比扫掠已经覆盖全表时，别再做一遍慢速滚轮回退（查找未命中是常见路径）
    if found is None and not covered:
        found = _sweep_wheel(hwnd, name, read_pane(hwnd))
    if found is None:
        _restore_position(hwnd, home_percent)
        logger.info("contacts.locate contact=%s mode=miss duration_ms=%.1f",
                    name, (time.monotonic() - started) * 1000)
        raise ContactNotFoundError(name)
    logger.info("contacts.locate contact=%s mode=scroll duration_ms=%.1f",
                name, (time.monotonic() - started) * 1000)
    return found


def _scroll_percent(hwnd: int) -> float | None:
    """当前列表滚动百分比；容器不发布 ScrollPattern 时返回 None。"""
    try:
        state = accessibility_scroll_state(hwnd)
    except AccessibilityUnavailableError:
        return None
    return state.position_percent if state.scrollable else None


def _restore_position(hwnd: int, percent: float | None) -> None:
    """把列表滚回查找开始前的位置（只用于查找失败后的收尾）。"""
    if percent is None:
        return
    try:
        accessibility_set_scroll_percent(hwnd, percent)
    except AccessibilityUnavailableError:
        return


# ── 对外操作 ────────────────────────────────────────────────────────
def activate(contact_name: str, hwnd: int | None = None) -> str:
    """按昵称定位并激活会话，返回昵称（不切换到别的会话）。

    已经是当前会话时直接返回，不做任何点击（真机实测点击已激活的行会把激活高亮
    切掉）；否则点击命中行中心，并用聊天区标题（失败时用激活行标记）复核。
    """
    name = normalize_name(contact_name)
    hwnd = hwnd or ensure_qq_window_with_retry()._hWnd
    started = time.monotonic()
    pane = read_pane(hwnd)
    if _selected_name(hwnd) == name and _contact_name_of(hwnd) == name:
        logger.info("contacts.activate contact=%s mode=already duration_ms=%.1f",
                    name, (time.monotonic() - started) * 1000)
        return name
    row = locate(name, hwnd=hwnd)
    x, y = row.center
    # 保留置顶：无激活置顶只改 Z 序，不移动窗口、不抢焦点。
    with WindowCaptureCtx(hwnd):
        click_at(x, y, restore=False)
    # 点击到 QQ 画出目标会话之间有重绘延迟：确认窗口内轮询两个独立信号——
    # 1) 聊天区标题 == 目标名（常规好友/群）；
    # 2) 目标行带上 --selected 激活标记（官方号、临时会话的标题可能与行名不同，
    #    标题读取也可能撞上 COM 瞬态失败）。
    # 两个信号在每一轮都判，不做"先只信标题、超时再补判标记"——真机实测后者会把
    # 一次瞬态 COM 失败放大成 ACTIVATION_FAILED 假阴性。
    deadline = time.monotonic() + ACTIVATE_CONFIRM_TIMEOUT_SECONDS
    actual = ""
    while True:
        actual = _contact_name_of(hwnd)
        if actual == name:
            logger.info("contacts.activate contact=%s mode=click duration_ms=%.1f",
                        name, (time.monotonic() - started) * 1000)
            return name
        if _selected_name(hwnd) == name:
            logger.info("contacts.activate contact=%s mode=click-selected duration_ms=%.1f",
                        name, (time.monotonic() - started) * 1000)
            return name
        if time.monotonic() >= deadline:
            raise ContactActivationError(name, actual)
        time.sleep(ACTIVATE_CONFIRM_INTERVAL_SECONDS)


def list_contacts(contact_name: str | None = None,
                  include_non_copyable: bool = False) -> dict[str, dict]:
    """读会话名单快照；指定昵称时先按昵称滚动定位（未找到抛 ContactNotFoundError）。

    默认只返回能读消息的会话（见 snapshot）；``include_non_copyable=True`` 用于诊断。
    """
    hwnd = ensure_qq_window_with_retry()._hWnd
    if contact_name:
        locate(contact_name, hwnd=hwnd)
    return snapshot(hwnd, include_non_copyable=include_non_copyable)


def ensure_message_copy_supported(contact_name: str, hwnd: int | None = None) -> None:
    """确认目标会话支持框选复制；不支持时抛 ContactNotCopyableError。

    只读当前已渲染的行（调用点通常是刚激活完目标会话、目标行必在视口内），不做滚动
    查找：这条检查只为了把"QQ 没给复制项"从 4 秒的失败路径提前成毫秒级明确报错。
    无障碍树读不到时直接放过，让原有的框选复制流程给出真实错误。
    """
    name = normalize_name(contact_name)
    # 已经学过"QQ 拒绝复制"的会话不必再读一次树：毫秒级报同一个业务错误。
    if is_refused(name):
        raise ContactNotCopyableError(name)
    hwnd = hwnd or ensure_qq_window_with_retry()._hWnd
    try:
        pane = accessibility_pane(hwnd)
    except AccessibilityUnavailableError:
        return
    row, _count = _match(pane, name)
    if row is not None and not row.copyable:
        raise ContactNotCopyableError(name)


def current_contact_name(hwnd: int | None = None) -> str | None:
    """当前会话名（聊天区标题）；读不到时返回 None（无障碍树不可用）。"""
    hwnd = hwnd or ensure_qq_window_with_retry()._hWnd
    name = _contact_name_of(hwnd)
    return name or None
