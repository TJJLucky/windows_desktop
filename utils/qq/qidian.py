"""Queue-owned qidian wake: verify website QQ once, then reuse accessible contact name."""

from __future__ import annotations

# hashlib：算链接指纹（同链去重与映射查询的键）
import hashlib
# logging：逐阶段耗时日志
import logging
# re：QQ 号格式校验
import re
# threading：保证浏览器唤醒槽一次只服务一个映射
import threading
# time：轮询与总预算
import time
# datetime：wokeAt（带时区偏移的 ISO 8601）
from datetime import datetime
# urllib.parse：校验并规范化唤醒链接（解析 scheme/host/path）
from urllib.parse import urlsplit

logger = logging.getLogger("qq_service.qidian")

# qidian 联系链接唯一合法的主机与路径（与客户端 isHqewContactUrl() 对齐）
QIDIAN_HOST = "qidianqq.hqew.com"
QIDIAN_PATH = "/GoClient"
# 唤醒后等待会话稳定的总预算（秒），含 QQ 窗口就绪等待与最后的 QQ 号识别
WAKE_TIMEOUT_BUDGET_SECONDS = 25.0
# 轮询会话名单的间隔（秒）
WAKE_POLL_INTERVAL_SECONDS = 0.15
# QQ 号格式（与 /v1/contacts/current:identify 契约一致）
_QQ_PATTERN = re.compile(r"^[1-9][0-9]{4,19}$")

# 浏览器唤醒始终一对一：打开一个、关闭一个，再允许下一个映射进入。
_QIDIAN_BROWSER_LOCK = threading.Lock()


class QidianError(RuntimeError):
    """qidian 唤醒链路的业务失败；``code`` 是 ``QQ_QIDIAN_*`` 错误码。"""

    def __init__(self, code: str, message: str = "", *, stage: str | None = None,
                 observed_qq_number: str | None = None, target_qq_number: str | None = None) -> None:
        self.code = code
        self.stage = stage
        self.observed_qq_number = observed_qq_number
        self.target_qq_number = target_qq_number
        super().__init__(f"{code}: {message}" if message else code)

    def public_detail(self) -> str:
        """只公开固定阶段和已校验号码，底层异常原文保留在服务日志。"""
        stages = {"session_ready", "active_row", "copy_number", "compare_number", "confirm_active"}
        parts = [f"阶段 {self.stage}："] if self.stage in stages else []
        if self.code == "QQ_QIDIAN_NUMBER_MISMATCH":
            observed, target = self.observed_qq_number, self.target_qq_number
            if isinstance(observed, str) and isinstance(target, str) and _QQ_PATTERN.fullmatch(observed) and _QQ_PATTERN.fullmatch(target):
                parts.append(f"当前接待人号码 {observed} 与网站号码 {target} 不一致")
        return "".join(parts)


def normalize_link(link: str) -> str:
    """校验并规范化 qidian 链接；非法时抛 ``QQ_QIDIAN_LINK_INVALID``。

    规则与客户端 ``isHqewContactUrl()`` 一致：协议只允许 http/https，禁止 userinfo、
    禁止端口，主机必须精确等于 ``qidianqq.hqew.com``（挡掉 ``xxx.qidianqq.hqew.com``
    与 ``qidianqq.hqew.com.evil.test`` 这类欺骗），路径必须精确等于 ``/GoClient``。
    """
    candidate = (link or "").strip()
    if not candidate:
        raise QidianError("QQ_QIDIAN_LINK_INVALID", "链接为空")
    try:
        parts = urlsplit(candidate)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        path = parts.path
        port = parts.port
        userinfo = bool(parts.username or parts.password)
    except ValueError as exc:
        raise QidianError("QQ_QIDIAN_LINK_INVALID", f"链接无法解析：{exc}") from exc

    if scheme not in ("http", "https"):
        raise QidianError("QQ_QIDIAN_LINK_INVALID", f"不接受的协议：{parts.scheme or '(空)'}")
    if userinfo:
        raise QidianError("QQ_QIDIAN_LINK_INVALID", "链接不能携带用户名/密码")
    if port is not None:
        raise QidianError("QQ_QIDIAN_LINK_INVALID", f"链接不能携带端口：{port}")
    if host != QIDIAN_HOST or path != QIDIAN_PATH:
        raise QidianError(
            "QQ_QIDIAN_LINK_INVALID",
            f"只接受 {QIDIAN_HOST}{QIDIAN_PATH} 链接，实际为 {host}{path or '/'}",
        )
    return candidate


def link_hash(link: str) -> str:
    """链接指纹（sha256 十六进制），用于同链去重与映射查询。"""
    return hashlib.sha256(normalize_link(link).encode("utf-8")).hexdigest()


def require_target_qq(provided: str | None) -> str:
    """校验调用方传入的目标 QQ 号；缺失或格式非法抛 ``QQ_QIDIAN_QQ_UNRESOLVED``。

    目标号**只**来自调用方（客户端从华强搜索结果页行内 ``a.customerqq[qq]`` 读到的商家 QQ 号），
    服务端不从链接里解析——链接里的号是企点路由/客服号，与 QQ 客户端里显示的接待人不是一回事。
    HTTP 契约已把该字段设为必填且带 pattern，这里是第二道防线（内部调用方也可能绕过路由层）。
    """
    caller = (provided or "").strip()
    if not caller:
        raise QidianError("QQ_QIDIAN_QQ_UNRESOLVED",
                          "调用方没有传目标 QQ 号（应传入华强搜索结果页读到的商家 QQ 号）")
    if not _QQ_PATTERN.match(caller):
        raise QidianError("QQ_QIDIAN_QQ_UNRESOLVED", f"调用方提供的目标 QQ 号格式不合法：{caller[:32]!r}")
    logger.info("qidian.target qq=%s", caller)
    return caller


def _ensure_qq_window(deadline: float):
    """使用唤醒链的同一截止时间等待真实会话列表，不额外延长预算。"""
    from .window_ops import QQWindowNotReadyError, ensure_qq_session_ready

    try:
        return ensure_qq_session_ready(deadline=deadline)
    except QQWindowNotReadyError as exc:
        raise QidianError("QQ_QIDIAN_WAKE_FAILED", f"QQ 主窗口不可用：{exc}") from exc


def _identify_active_qq(hwnd: int, deadline: float) -> str | None:
    """复制当前激活行的 QQ 号；把底层的 ``QQ_CONTACT_NUMBER_*`` 映射成 ``QQ_QIDIAN_*``。"""
    from .contact_number import ContactNumberReader

    try:
        return ContactNumberReader().identify_current(
            deadline=deadline, expected_hwnd=hwnd, require_unique_name=False)
    except RuntimeError as exc:
        code = str(exc)
        if "QQ_CONTACT_NUMBER_ACTIVE_AMBIGUOUS" in code:
            raise QidianError("QQ_QIDIAN_ACTIVE_AMBIGUOUS", "激活行不唯一，无法确定接待人 QQ 号") from exc
        if "QQ_CONTACT_NUMBER_ACTIVE_NOT_FOUND" in code:
            raise QidianError("QQ_QIDIAN_ACTIVE_NOT_FOUND", "读不到当前激活行") from exc
        raise QidianError("QQ_QIDIAN_SESSION_NOT_STABLE", f"识别接待人 QQ 号失败：{code}") from exc


def _active_name(hwnd):
    from .contact_lookup import read_pane
    pane = read_pane(hwnd)
    selected = pane.selected_rows()
    if len(selected) != 1:
        code = "QQ_QIDIAN_ACTIVE_AMBIGUOUS" if selected else "QQ_QIDIAN_ACTIVE_NOT_FOUND"
        raise QidianError(code, f"需要唯一激活会话（名单 {len(pane.rows)} 行，激活 {len(selected)} 行）")
    return selected[0].name

def wake_and_identify(link: str, target_qq_number: str, *,
                      timeout: float = WAKE_TIMEOUT_BUDGET_SECONDS) -> dict:
    from .qidian_browser import QidianBrowserWindow

    with _QIDIAN_BROWSER_LOCK:
        browser = QidianBrowserWindow(normalize_link(link), target_qq_number)
        try:
            return _wake_and_identify(link, target_qq_number, browser, timeout=timeout)
        finally:
            try:
                browser.close()
            except Exception as exc:
                logger.warning("qidian.browser.cleanup_failed type=%s", type(exc).__name__)


def _wake_and_identify(link: str, target_qq_number: str, browser, *,
                      timeout: float = WAKE_TIMEOUT_BUDGET_SECONDS) -> dict:
    """Open once; mismatches and transient tree states are polled until the deadline."""
    normalized = normalize_link(link)
    target = require_target_qq(target_qq_number)
    deadline = time.monotonic() + timeout
    hwnd = _ensure_qq_window(deadline)._hWnd
    from .qidian_refusal import QidianRefusalProbe
    refusal = QidianRefusalProbe(target)
    try:
        browser.open()
        logger.info("qidian.wake.dispatched target=%s awaiting=two_matching_observations", target)
    except OSError as exc:
        raise QidianError("QQ_QIDIAN_WAKE_FAILED", str(exc)) from exc
    # Let the protocol handoff reveal an add-only profile before window recovery
    # foregrounds QQ and dismisses it. This remains inside the same total budget.
    grace_deadline = min(deadline, time.monotonic() + 2.5)
    while time.monotonic() < grace_deadline:
        if refusal.poll() is not None:
            raise QidianError("QQ_QIDIAN_FRIEND_REQUIRED", "目标商家仅显示加好友入口，已跳过")
        time.sleep(min(0.15, max(0, grace_deadline - time.monotonic())))
    # GoClient opens a browser above QQ. Keep each observation visible so QQ can
    # repaint its accessibility provider; visibility itself is never identity proof.
    from ..core.window import WindowCaptureCtx
    started = time.monotonic()
    last_error = "尚未读到接待人"
    previous = None
    # First-time mapping requires two full name/number observations, separated by
    # the existing poll interval. It deliberately does not scan the whole list for
    # name uniqueness; the active row and the website QQ number are the identity.
    verified_pair = None
    attempt = 0
    number_mismatch = False
    number = None
    stage = "active_row"
    while time.monotonic() < deadline:
        attempt += 1
        # Outside the transient-UIA retry catch: explicit refusal is terminal.
        if refusal.poll() is not None:
            raise QidianError("QQ_QIDIAN_FRIEND_REQUIRED", "目标商家需先添加好友或仅显示加好友入口，已跳过")
        try:
            stage = "session_ready"
            current_hwnd = _ensure_qq_window(deadline)._hWnd
            if current_hwnd != hwnd:
                logger.info("qidian.wake.window_changed previous=%s current=%s", hwnd, current_hwnd)
                hwnd = current_hwnd
                previous = None
                verified_pair = None
            with WindowCaptureCtx(hwnd):
                stage = "active_row"
                name = _active_name(hwnd)
                if previous != name:
                    verified_pair = None
                if previous == name:
                    stage = "copy_number"
                    number = _identify_active_qq(hwnd, deadline)
                    stage = "compare_number"
                    matched = number == target
                    number_mismatch = number is not None and not matched
                    logger.info("qidian.wake.compare attempt=%d contact=%r observed=%s target=%s matched=%s elapsed_ms=%.1f",
                                attempt, name, number, target, matched,
                                (time.monotonic() - started) * 1000)
                    # Copying may overlap QQ repaint: bind the number to the same row.
                    if matched:
                        stage = "confirm_active"
                        if _active_name(hwnd) != name:
                            raise QidianError("QQ_QIDIAN_SESSION_NOT_STABLE", "复制号码后激活会话发生变化")
                        pair = (name, number)
                        confirmed = verified_pair == pair
                        verified_pair = pair
                        logger.info("qidian.wake.confirmation attempt=%d contact=%r qq=%s consecutive=%d required=2",
                                    attempt, name, number, 2 if confirmed else 1)
                        if confirmed:
                            # The target row is already selected and the website
                            # QQ number has matched twice. Scanning the complete
                            # list for name uniqueness can rebuild the UIA tree and
                            # burn the budget; re-activating the same session is
                            # unnecessary and would force another copy for no gain.
                            stage = "confirm_active"
                            if _active_name(hwnd) != name or time.monotonic() >= deadline:
                                raise QidianError("QQ_QIDIAN_SESSION_NOT_STABLE", "二次核号后激活会话变化或超过预算")
                            logger.info("qidian.wake.verified contact=%r qq=%s elapsed_ms=%.1f", name, number,
                                        (time.monotonic() - started) * 1000)
                            return dict(linkHash=link_hash(normalized), contactName=name, qqNumber=number,
                                        targetQqNumber=target, qqNumberVerified=True, activeStable=True,
                                        reused=False, wokeAt=datetime.now().astimezone().isoformat(timespec="seconds"))
                        last_error = "首次核号一致，等待下一轮确认同一会话及号码"
                    else:
                        verified_pair = None
                        last_error = ("当前会话无法复制 QQ 号" if number is None else
                                      f"当前接待人号码 {number} 与网站号码 {target} 不一致")
                previous = name
        except Exception as exc:
            if isinstance(exc, QidianError) and exc.code == "QQ_QIDIAN_NUMBER_MISMATCH":
                raise
            # UIA may temporarily rebuild while GoClient opens the conversation.
            previous = None
            verified_pair = None
            last_error = str(exc)
            logger.info("qidian.wake.retry attempt=%d stage=%s elapsed_ms=%.1f reason=%s",
                        attempt, stage, (time.monotonic() - started) * 1000, exc)
        time.sleep(min(WAKE_POLL_INTERVAL_SECONDS, max(0, deadline - time.monotonic())))
    logger.warning("qidian.wake.timeout stage=%s target=%s reason=%s", stage, target, last_error)
    code = "QQ_QIDIAN_NUMBER_MISMATCH" if number_mismatch else "QQ_QIDIAN_SESSION_NOT_STABLE"
    raise QidianError(code, f"阶段 {stage}：{last_error}", stage=stage,
                      observed_qq_number=number, target_qq_number=target)


def resolve_verified(link: str, target_qq_number: str, metadata: dict, mappings, *, force_reverify: bool = False) -> dict:
    """Called only inside the Dispatcher: activation and persistence share the desktop slot."""
    from .contact_lookup import restore_verified_contact
    audit = metadata.get("audit") or {}
    logger.info("qidian.resolve.begin batch=%s target_id=%s", audit.get("batchId"), audit.get("targetId"))
    target = require_target_qq(target_qq_number)
    normalized = normalize_link(link)
    scope = metadata['account_scope']
    if force_reverify:
        mappings.invalidate(scope, target)
        logger.info("qidian.reverify.forced batch=%s target_id=%s", audit.get("batchId"), audit.get("targetId"))
    cached = None if force_reverify else mappings.conversation(scope, target)
    result = None
    if cached:
        try:
            hwnd = _ensure_qq_window(time.monotonic() + WAKE_TIMEOUT_BUDGET_SECONDS)._hWnd
            name = cached['contact_name']
            restore_verified_contact(name, hwnd=hwnd, deadline=time.monotonic() + WAKE_TIMEOUT_BUDGET_SECONDS)
            if _active_name(hwnd) != name:
                raise QidianError("QQ_QIDIAN_ACTIVE_NOT_FOUND", "复用激活结果不一致")
            time.sleep(WAKE_POLL_INTERVAL_SECONDS)
            if _active_name(hwnd) != name:
                raise QidianError("QQ_QIDIAN_SESSION_NOT_STABLE", "复用激活不稳定")
            result = dict(linkHash=link_hash(normalized), contactName=name, qqNumber=target,
                          targetQqNumber=target, qqNumberVerified=True, activeStable=True,
                          reused=True, wokeAt=datetime.now().astimezone().isoformat(timespec="seconds"))
            logger.info("qidian.reuse.activated scope=%s contact=%r qq=%s", scope, name, target)
        except Exception as exc:
            mappings.invalidate(scope, target)
            logger.info("qidian.reuse.invalidated reason=%s", exc)
    if result is None:
        result = wake_and_identify(normalized, target)
    mappings.record_verified(**metadata, result=result)
    return {**result, 'accountScope': scope, 'siteId': metadata['site_id'],
            'supplierId': metadata['supplier_id'], 'supplierName': metadata['supplier_name']}
