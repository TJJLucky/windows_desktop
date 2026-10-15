"""qidian 链接唤醒 QQ 并识别当前接待人（服务端自唤醒链路）。

链路（整段都在 Dispatcher 单消费者队列里串行执行，杜绝「多个 qidian 同时唤醒导致
接待人对应错乱」）：

1. **链接校验**：只接受 ``http(s)://qidianqq.hqew.com/GoClient``——拒 userinfo、端口、
   其他路径、子域变体与其他协议，与客户端 ``isHqewContactUrl()`` 同一条规则；
2. **解析目标 QQ 号**：先取链接自身 query 参数 ``QQ``；取不到时 POST 华强 qidian 的
   ``QidianApi/User/GetQQLinkUrl``（原样透传链接的全部 query 参数），从返回 ``Data``
   里的 ``wpa.qq.com/msgrd?...&uin=`` 提取；两者都拿不到即业务失败，**不唤醒**；
3. **唤醒**：QQ 进程不存在时先走 ``window_ops.start_qq()``，再 ``os.startfile(链接)``
   交给默认浏览器（与客户端现网已跑通的路径一致，不反向构造 ``tencent://``）；
4. **等会话稳定**：轮询无障碍树，直到「连续两次读到同一份会话名单 + 唯一激活行」；
5. **识别接待人**：用 ``ContactNumberReader.identify_current()`` 复制当前激活行的 QQ 号；
   群会话 / 不提供「复制 QQ 号」菜单项的会话返回 ``None``（不报错）。

总等待上限 ``WAKE_TIMEOUT_BUDGET_SECONDS``（约 25 秒，含窗口就绪等待）；超时按
``QQ_QIDIAN_SESSION_NOT_STABLE`` 失败，绝不写脏映射。座席判定不做启发式猜测：
多激活行就是 ``QQ_QIDIAN_ACTIVE_AMBIGUOUS``，读不到激活行就是 ``QQ_QIDIAN_ACTIVE_NOT_FOUND``。
"""

from __future__ import annotations

# hashlib：算链接指纹（同链去重与映射查询的键）
import hashlib
# logging：逐阶段耗时日志
import logging
# os：startfile 把链接交给默认浏览器
import os
# re：QQ 号格式校验
import re
# time：轮询与总预算
import time
# datetime：wokeAt（带时区偏移的 ISO 8601）
from datetime import datetime
# typing：行签名等泛型标注
from typing import Any
# urllib.parse：解析链接 query（含 qidian 接口透传参数）
from urllib.parse import parse_qs, urlsplit

# httpx：调用 qidian 的 GetQQLinkUrl 兜底解析接口
import httpx

logger = logging.getLogger("qq_service.qidian")

# qidian 联系链接唯一合法的主机与路径（与客户端 isHqewContactUrl() 对齐）
QIDIAN_HOST = "qidianqq.hqew.com"
QIDIAN_PATH = "/GoClient"
# 把 query 参数换算成 wpa.qq.com 会话链接的接口（链接自身没有 QQ 参数时的兜底）
QIDIAN_QQ_API = f"https://{QIDIAN_HOST}/QidianApi/User/GetQQLinkUrl"
# 解析接口超时（秒）；解析视为尽力而为，只有「链接没有 QQ 且接口也拿不到」才判硬失败
QQ_RESOLVE_TIMEOUT_SECONDS = 8.0
# 唤醒后等待会话稳定的总预算（秒），含 QQ 窗口就绪等待与最后的 QQ 号识别
WAKE_TIMEOUT_BUDGET_SECONDS = 25.0
# 轮询会话名单的间隔（秒）
WAKE_POLL_INTERVAL_SECONDS = 0.15
# 名单已稳定却迟迟没有「唯一激活行」时的宽限（秒）：超过就按具体原因失败，不空等满预算
SELECTION_GRACE_SECONDS = 6.0
# QQ 号格式（与 /v1/contacts/current:identify 契约一致）
_QQ_PATTERN = re.compile(r"^[1-9][0-9]{4,19}$")


class QidianError(RuntimeError):
    """qidian 唤醒链路的业务失败；``code`` 是 ``QQ_QIDIAN_*`` 错误码。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


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


def _query_params(link: str) -> dict[str, str]:
    """链接的全部 query 参数（同名多值取第一个），原样透传给 qidian 接口。"""
    return {key: values[0] for key, values in parse_qs(urlsplit(link).query, keep_blank_values=True).items()}


def extract_target_qq(link: str) -> str | None:
    """链接自身携带的 QQ 参数（``QQ``，兼容小写 ``qq``）；没有或格式非法返回 None。"""
    params = _query_params(link)
    for key in ("QQ", "qq"):
        value = (params.get(key) or "").strip()
        if _QQ_PATTERN.match(value):
            return value
    return None


def _qq_from_wpa_link(value: str) -> str | None:
    """从 ``wpa.qq.com/msgrd?...&uin=`` 形态的链接里取 uin，取不到返回 None。"""
    try:
        values = parse_qs(urlsplit(value).query).get("uin") or []
    except ValueError:
        return None
    uin = values[0].strip() if values else ""
    return uin if _QQ_PATTERN.match(uin) else None


def resolve_target_qq(link: str) -> str | None:
    """解析目标 QQ 号：先取链接参数 ``QQ``，再用 qidian 接口兜底；都拿不到返回 None。"""
    direct = extract_target_qq(link)
    if direct:
        logger.info("qidian.resolve source=link qq=%s", direct)
        return direct

    payload = _query_params(link)
    try:
        response = httpx.post(
            QIDIAN_QQ_API,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=QQ_RESOLVE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body: Any = response.json()
    except Exception as exc:  # 网络/HTTP/JSON 任何问题都只意味「这条兜底路走不通」
        logger.warning("qidian.resolve.api_failed error=%s", exc)
        return None

    data = body.get("Data") if isinstance(body, dict) else None
    resolved = _qq_from_wpa_link(data) if isinstance(data, str) else None
    if resolved:
        logger.info("qidian.resolve source=api qq=%s", resolved)
        return resolved
    logger.warning(
        "qidian.resolve.api_empty error=%s message=%s",
        (body or {}).get("Error") if isinstance(body, dict) else None,
        (body or {}).get("Message") if isinstance(body, dict) else None,
    )
    return None


def _pane_signature(pane) -> tuple:
    """名单指纹（行名 + 屏幕矩形）：与 contact_lookup 的口径一致。"""
    return tuple((row.name, row.rect) for row in pane.rows)


def _ensure_qq_window(deadline: float):
    """保证 QQ 主窗口就绪；预算内起不来就抛 ``QQ_QIDIAN_WAKE_FAILED``。

    QQ 进程不存在时先 ``start_qq()``（与既有 ``ensure_qq_window_with_retry`` 同一路径），
    冷启动可能超过它自带的约 2 秒重试窗口，因此这里按总预算重试。
    """
    from .window_ops import (QQWindowNotReadyError, ensure_qq_window_with_retry,
                             get_qq_pids, start_qq)

    if not get_qq_pids():
        logger.info("qidian.wake.start_qq")
        start_qq()
    while True:
        try:
            return ensure_qq_window_with_retry()
        except QQWindowNotReadyError as exc:
            if time.monotonic() >= deadline:
                raise QidianError("QQ_QIDIAN_WAKE_FAILED", f"QQ 主窗口不可用：{exc}") from exc
            logger.info("qidian.wake.window_retry error=%s", exc)
            time.sleep(1.0)


def _wait_active_contact(hwnd: int, deadline: float) -> str:
    """等到「一份稳定名单 + 唯一激活行」，返回激活行的会话名。

    复用 ``contact_lookup.read_pane`` 的稳定判定（连续两次读到同一份名单），并额外要求
    有且只有一行处于激活态。超时按最后观测到的状态报具体错误，不猜测接待人。
    """
    from .accessibility import AccessibilityUnavailableError
    from .contact_lookup import read_pane

    previous: tuple | None = None
    stable_since: float | None = None
    selected_count = 0
    while time.monotonic() < deadline:
        try:
            pane = read_pane(hwnd)
        except AccessibilityUnavailableError as exc:
            # 元素树重建期读不到：不算稳定，继续等到预算用尽
            logger.warning("qidian.wake.pane_unavailable error=%s", exc)
            previous = None
            stable_since = None
            time.sleep(WAKE_POLL_INTERVAL_SECONDS)
            continue

        signature = _pane_signature(pane)
        selected = pane.selected_rows()
        selected_count = len(selected)
        now = time.monotonic()
        if pane.rows and signature == previous:
            if selected_count == 1:
                return selected[0].name
            # 名单已稳定但没有唯一激活行：给一段宽限等激活高亮画出来，超时就按原因报错
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= SELECTION_GRACE_SECONDS:
                break
        else:
            stable_since = None
        previous = signature
        time.sleep(WAKE_POLL_INTERVAL_SECONDS)

    if selected_count > 1:
        raise QidianError("QQ_QIDIAN_ACTIVE_AMBIGUOUS",
                          f"会话名单里有 {selected_count} 行同时处于激活态，无法确定接待人")
    if selected_count == 0:
        raise QidianError("QQ_QIDIAN_ACTIVE_NOT_FOUND",
                          "会话名单里读不到激活行（QQ 可能没有打开该临时会话）")
    raise QidianError("QQ_QIDIAN_SESSION_NOT_STABLE",
                      f"{WAKE_TIMEOUT_BUDGET_SECONDS:.0f} 秒内未等到稳定的会话名单")


def _identify_active_qq() -> str | None:
    """复制当前激活行的 QQ 号；把底层的 ``QQ_CONTACT_NUMBER_*`` 映射成 ``QQ_QIDIAN_*``。"""
    from .contact_number import ContactNumberReader

    try:
        return ContactNumberReader().identify_current()
    except RuntimeError as exc:
        code = str(exc)
        if "QQ_CONTACT_NUMBER_ACTIVE_AMBIGUOUS" in code:
            raise QidianError("QQ_QIDIAN_ACTIVE_AMBIGUOUS", "激活行不唯一，无法确定接待人 QQ 号") from exc
        if "QQ_CONTACT_NUMBER_ACTIVE_NOT_FOUND" in code:
            raise QidianError("QQ_QIDIAN_ACTIVE_NOT_FOUND", "读不到当前激活行") from exc
        raise QidianError("QQ_QIDIAN_SESSION_NOT_STABLE", f"识别接待人 QQ 号失败：{code}") from exc


def wake_and_identify(link: str, *, timeout: float = WAKE_TIMEOUT_BUDGET_SECONDS) -> dict:
    """唤醒 qidian 链接对应的 QQ 会话并识别当前接待人。

    只在 Dispatcher 的 worker 线程内调用（串行消费队列），返回结果 dict；
    任何业务失败都抛 ``QidianError``（错误码 ``QQ_QIDIAN_*``）。
    """
    normalized = normalize_link(link)
    target_qq = resolve_target_qq(normalized)
    if not target_qq:
        raise QidianError("QQ_QIDIAN_QQ_UNRESOLVED",
                          "链接既没有 QQ 参数，qidian 接口也解析不出目标 QQ 号")

    started = time.monotonic()
    deadline = started + timeout
    window = _ensure_qq_window(deadline)
    hwnd = window._hWnd
    logger.info("qidian.wake.window_ready hwnd=%s elapsed_ms=%.1f",
                hwnd, (time.monotonic() - started) * 1000)

    wake_started = time.monotonic()
    try:
        # 交给默认浏览器打开（与客户端 openExternal 同一条已验证路径）
        os.startfile(normalized)
    except OSError as exc:
        raise QidianError("QQ_QIDIAN_WAKE_FAILED", f"无法打开 qidian 链接：{exc}") from exc

    contact_name = _wait_active_contact(hwnd, deadline)
    logger.info("qidian.wake.session_stable contact=%r wait_ms=%.1f",
                contact_name, (time.monotonic() - wake_started) * 1000)

    qq_number = _identify_active_qq()
    verified = qq_number is not None and qq_number == target_qq
    logger.info("qidian.wake.done contact=%r qq=%s target=%s verified=%s total_ms=%.1f",
                contact_name, qq_number, target_qq, verified, (time.monotonic() - started) * 1000)
    return {
        "linkHash": link_hash(normalized),
        "contactName": contact_name,
        "qqNumber": qq_number,
        "targetQqNumber": target_qq,
        "qqNumberVerified": verified,
        "activeStable": True,
        "wokeAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
