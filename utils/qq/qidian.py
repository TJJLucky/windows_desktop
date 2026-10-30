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
    """Verify one target QQ through a single controlled qidian handoff.

    The browser is opened once. A second controlled open is allowed only when
    the first eight seconds produced no QQ handoff signal at all. Once QQ has
    handed off, slow tree/copy states are polled until the same 25s deadline;
    they never cause another browser open.
    """
    normalized = normalize_link(link)
    target = require_target_qq(target_qq_number)
    started = time.monotonic()
    deadline = started + timeout
    hwnd = _ensure_qq_window(deadline)._hWnd

    from .qidian_refusal import QidianRefusalProbe, qidian_target_pages
    baseline_name = None
    try:
        baseline_name = _active_name(hwnd)
    except Exception as exc:
        logger.info("qidian.wake.baseline.unavailable hwnd=%s reason=%s", hwnd, exc)
    baseline_pages = set()
    try:
        baseline_pages = qidian_target_pages(target)
    except Exception as exc:
        logger.info("qidian.wake.browser_baseline.unavailable target=%s reason=%s", target, exc)

    refusal = QidianRefusalProbe(target)
    try:
        # Do not wait for browser-window discovery here. The handoff loop below
        # polls QQ and the browser in parallel and advances on the first signal.
        browser.open(wait_for_window=False)
    except OSError as exc:
        raise QidianError("QQ_QIDIAN_WAKE_FAILED", str(exc), stage="session_ready") from exc

    from ..core.window import WindowCaptureCtx

    activation_attempts = 1
    handoff_observed = False
    reopen_deadline = min(deadline, time.monotonic() + 8.0)
    next_page_scan = 0.0
    previous_name = None
    verified_pair = None
    number = None
    number_mismatch = False
    last_error = "尚未检测到 QQ 接管"
    stage = "session_ready"

    while time.monotonic() < deadline:
        now = time.monotonic()
        try:
            # Explicit add-friend/refusal evidence is terminal. It is a valid
            # early exit, not an identity guess.
            if refusal.poll() is not None:
                raise QidianError(
                    "QQ_QIDIAN_FRIEND_REQUIRED",
                    "目标商家仅提供加好友入口或暂不支持临时会话",
                    stage="session_ready",
                )

            # The qidian browser page itself is handoff evidence. A matching
            # page that existed before this wake is excluded by the baseline.
            if now >= next_page_scan:
                next_page_scan = now + 0.5
                try:
                    if qidian_target_pages(target) - baseline_pages:
                        handoff_observed = True
                except Exception as exc:
                    logger.info("qidian.handoff.page_scan_unavailable target=%s reason=%s", target, exc)

            current_hwnd = _ensure_qq_window(deadline)._hWnd
            if current_hwnd != hwnd:
                logger.info("qidian.wake.window_changed previous=%s current=%s", hwnd, current_hwnd)
                hwnd = current_hwnd
                baseline_name = None
                previous_name = None
                verified_pair = None
                handoff_observed = False

            with WindowCaptureCtx(hwnd):
                try:
                    name = _active_name(hwnd)
                except Exception as exc:
                    stage = "session_ready" if not handoff_observed else "active_row"
                    last_error = str(exc)
                    logger.info("qidian.wake.active_row_unavailable attempt=%d stage=%s elapsed_ms=%.1f reason=%s",
                                activation_attempts, stage, (time.monotonic() - started) * 1000, exc)
                    raise

                # A manual session switch can change the active row without
                # proving that the qidian link handed off. Treat a changed row
                # only as a candidate; the copied target number is the
                # authoritative confirmation and promotes it to a handoff.
                activation_candidate = handoff_observed or baseline_name is None or name != baseline_name
                if activation_candidate:
                    stage = "active_row"
                    if name != previous_name:
                        verified_pair = None
                    previous_name = name

                    # Once a unique active row and handoff are present, copy
                    # the number immediately. No full-list uniqueness scan is
                    # allowed on the hot path.
                    stage = "copy_number"
                    number = _identify_active_qq(hwnd, deadline)
                    stage = "compare_number"
                    matched = number == target
                    if matched:
                        handoff_observed = True
                    number_mismatch = handoff_observed and number is not None and not matched
                    logger.info(
                        "qidian.wake.compare attempt=%d contact=%r observed=%s target=%s matched=%s elapsed_ms=%.1f",
                        activation_attempts, name, number, target, matched,
                        (time.monotonic() - started) * 1000,
                    )
                    if matched:
                        stage = "confirm_active"
                        if _active_name(hwnd) != name:
                            raise QidianError(
                                "QQ_QIDIAN_SESSION_NOT_STABLE",
                                "复制号码后激活会话发生变化",
                                stage=stage,
                            )
                        pair = (name, number)
                        confirmed = verified_pair == pair
                        verified_pair = pair
                        logger.info(
                            "qidian.wake.confirmation attempt=%d contact=%r qq=%s consecutive=%d required=2",
                            activation_attempts, name, number, 2 if confirmed else 1,
                        )
                        if confirmed:
                            elapsed_ms = int(round((time.monotonic() - started) * 1000))
                            logger.info(
                                "qidian.wake.verified contact=%r qq=%s attempts=%d elapsed_ms=%d",
                                name, number, activation_attempts, elapsed_ms,
                            )
                            return {
                                "linkHash": link_hash(normalized),
                                "contactName": name,
                                "qqNumber": number,
                                "targetQqNumber": target,
                                "qqNumberVerified": True,
                                "activeStable": True,
                                "activationAttempts": activation_attempts,
                                "handoffObserved": True,
                                "activationStage": "confirm_active",
                                "elapsedMs": elapsed_ms,
                                "wokeAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                            }
                        last_error = "首次核号一致，等待下一轮确认同一会话及号码"
                    else:
                        verified_pair = None
                        last_error = (
                            "当前会话无法复制 QQ 号" if number is None
                            else f"当前接待人号码 {number} 与网站号码 {target} 不一致"
                        )
        except QidianError as exc:
            if exc.code == "QQ_QIDIAN_FRIEND_REQUIRED":
                raise
            last_error = str(exc)
            if exc.stage:
                stage = exc.stage
            previous_name = None
            verified_pair = None
            logger.info("qidian.wake.retry attempt=%d stage=%s elapsed_ms=%.1f reason=%s",
                        activation_attempts, stage, (time.monotonic() - started) * 1000, exc)
        except Exception as exc:
            # UIA may temporarily rebuild while GoClient opens the conversation.
            last_error = str(exc)
            previous_name = None
            verified_pair = None
            number = None
            logger.info("qidian.wake.retry attempt=%d stage=%s elapsed_ms=%.1f reason=%s",
                        activation_attempts, stage, (time.monotonic() - started) * 1000, exc)

        now = time.monotonic()
        if now >= reopen_deadline and not handoff_observed:
            if activation_attempts < 2 and now + 2.0 < deadline:
                logger.warning("qidian.wake.reopen target=%s attempt=%d elapsed_ms=%.1f reason=no_handoff",
                               target, activation_attempts, (now - started) * 1000)
                try:
                    browser.close()
                    browser.open(wait_for_window=False)
                except OSError as exc:
                    raise QidianError("QQ_QIDIAN_WAKE_FAILED", str(exc), stage="session_ready") from exc
                activation_attempts += 1
                reopen_deadline = min(deadline, time.monotonic() + 8.0)
                previous_name = None
                verified_pair = None
                number = None
                number_mismatch = False
                last_error = "第一次打开后未检测到 QQ 接管，已受控重开一次"
                stage = "session_ready"
                continue
            if activation_attempts >= 2:
                raise QidianError(
                    "QQ_QIDIAN_WAKE_FAILED",
                    "两次打开链接后仍未检测到 QQ 接管",
                    stage="session_ready",
                )
        time.sleep(min(WAKE_POLL_INTERVAL_SECONDS, max(0, deadline - time.monotonic())))

    logger.warning("qidian.wake.timeout stage=%s target=%s attempts=%d reason=%s",
                   stage, target, activation_attempts, last_error)
    if not handoff_observed:
        code = "QQ_QIDIAN_WAKE_FAILED"
    elif number_mismatch:
        code = "QQ_QIDIAN_NUMBER_MISMATCH"
    else:
        code = "QQ_QIDIAN_SESSION_NOT_STABLE"
    raise QidianError(code, f"阶段 {stage}：{last_error}", stage=stage,
                      observed_qq_number=number, target_qq_number=target)


def resolve_verified(link: str, target_qq_number: str, metadata: dict, mappings) -> dict:
    """Run one fresh qidian verification inside the Dispatcher slot.

    Cached-name reuse is intentionally not part of this endpoint. A successful
    verification is persisted; later reuse decisions happen in the client via
    contacts:activate and the shared contact snapshot.
    """
    audit = metadata.get("audit") or {}
    logger.info("qidian.resolve.begin batch=%s target_id=%s", audit.get("batchId"), audit.get("targetId"))
    target = require_target_qq(target_qq_number)
    normalized = normalize_link(link)
    result = wake_and_identify(normalized, target)
    mappings.record_verified(**metadata, result=result)
    return {**result, 'accountScope': metadata['account_scope'],
            'siteId': metadata['site_id'], 'supplierId': metadata['supplier_id'],
            'supplierName': metadata['supplier_name']}
