"""qidian 链接唤醒 QQ 并识别当前接待人（服务端自唤醒链路）。

链路（整段都在 Dispatcher 单消费者队列里串行执行，杜绝「多个 qidian 同时唤醒导致
接待人对应错乱」）：

1. **链接校验**：只接受 ``http(s)://qidianqq.hqew.com/GoClient``——拒 userinfo、端口、
   其他路径、子域变体与其他协议，与客户端 ``isHqewContactUrl()`` 同一条规则；
2. **确定目标 QQ 号** ``resolve_target_qq()``：**优先用调用方传入的号**
   （``targetQqNumber``——客户端从华强搜索/报价结果页读到的商家 QQ 号，``a.customerqq[qq]``）。
   真机实测链接自身的 ``QQ`` 参数并不可靠：另一种形态 ``wpa1.qq.com/<短码>`` 跟随 302 后拿到的是
   企点路由/客服号 ``kfuin``（真机：目标 3009004623 而 QQ 客户端显示的接待人是 800885759），
   站点 ``GetQQLinkUrl`` 返回的也是同一个路由号，两者都不能当作接待人 QQ 号。因此链接解析只在
   调用方**没有**传号时兜底（链接 ``QQ`` 参数 → ``GetQQLinkUrl``）；两种来源都拿不到即业务失败
   （``QQ_QIDIAN_QQ_UNRESOLVED``），**不唤醒**；
3. **唤醒**：QQ 进程不存在时先走 ``window_ops.start_qq()``，再 ``os.startfile(链接)``
   交给默认浏览器（与客户端现网已跑通的路径一致，不反向构造 ``tencent://``）；
4. **等会话稳定**：轮询无障碍树——先等名单**相对唤醒前发生变化**（浏览器起页 + GoClient
   跳转 + QQ 把目标会话顶到最前，真机实测有数秒延迟），再等名单在 ``WAKE_SETTLE_SECONDS``
   内不再变化且出现唯一激活行；
5. **识别接待人**：用 ``ContactNumberReader.identify_current()`` 复制当前激活行的 QQ 号；
   群会话 / 不提供「复制 QQ 号」菜单项的会话返回 ``None``（不报错）；
6. **生效判定（防脏映射）**：唤醒前先记下当时的会话名单指纹与激活行名。识别值等于目标 QQ 号
   → 通过（``qqNumberVerified=true``）；名单在唤醒后发生了变化（会话被顶到
   最前 / 新会话出现）→ 也通过（``qqNumberVerified=false``，接待人可能换了号或本身是客服号）；
   名单与唤醒前**一模一样**且读到的 QQ 号与目标对不上 → 判定链接未真正生效，报
   ``QQ_QIDIAN_WAKE_FAILED`` 并**拒绝写映射**，绝不让「上一次的激活行」冒充本次接待人。

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
# urllib.parse：解析链接 query（含 qidian 接口透传参数）与 QQ 参数里的转义
from urllib.parse import parse_qs, unquote, urlsplit

# httpx：调用 qidian 的 GetQQLinkUrl 解析接口，以及跟随 wpa1 短链的 302
import httpx

logger = logging.getLogger("qq_service.qidian")

# qidian 联系链接唯一合法的主机与路径（与客户端 isHqewContactUrl() 对齐）
QIDIAN_HOST = "qidianqq.hqew.com"
QIDIAN_PATH = "/GoClient"
# 把 query 参数换算成唤醒链接的接口（GoClient 页面 JS 自己调的就是这个）
QIDIAN_QQ_API = f"https://{QIDIAN_HOST}/QidianApi/User/GetQQLinkUrl"
# 页面 JS 传给 GetQQLinkUrl 的参数键（缺的补空串，与页面行为一致）
QIDIAN_API_PARAM_KEYS = (
    "Model", "Brand", "Package", "Remark", "LotNo",
    "Num", "QQ", "BHqewUserID", "CHqewUserID", "Signature", "BussinessType",
)
# 唤醒链接里目标号可能落在的 query 键：uin= 直连号，kfuin= 企点客服号
_WAKE_QQ_QUERY_KEYS = ("uin", "kfuin")
# 需要跟随 302 才能拿到 kfuin 的短链主机
_WPA_SHORT_LINK_HOSTS = ("wpa1.qq.com",)
# 解析接口 / 短链跳转的超时（秒）；解析视为尽力而为，只有「接口和链接都拿不到」才判硬失败
QQ_RESOLVE_TIMEOUT_SECONDS = 8.0
# 唤醒后等待会话稳定的总预算（秒），含 QQ 窗口就绪等待与最后的 QQ 号识别
WAKE_TIMEOUT_BUDGET_SECONDS = 25.0
# 轮询会话名单的间隔（秒）
WAKE_POLL_INTERVAL_SECONDS = 0.15
# 等链接生效的预算（秒）：浏览器起页 + GoClient 跳转 + QQ 把会话顶到最前
WAKE_CHANGE_BUDGET_SECONDS = 15.0
# 名单最后一次变化后需保持多久不变才认为「会话已稳定」（秒）
WAKE_SETTLE_SECONDS = 1.5
# QQ 号识别最多尝试几次：会话刚被顶到最前时名单还在重绘，色带判定会瞬时读不到激活行
IDENTIFY_ATTEMPTS = 3
# 识别重试前要求的最小剩余预算（秒），太少就直接按最后一次错误收尾
IDENTIFY_MIN_REMAINING_SECONDS = 2.0
# 识别环节可以重试的错误码（名单重绘期的瞬时态，不是业务失败）
_IDENTIFY_RETRY_CODES = ("QQ_QIDIAN_ACTIVE_NOT_FOUND", "QQ_QIDIAN_ACTIVE_AMBIGUOUS")
# QQ 号格式（与 /v1/contacts/current:identify 契约一致）
_QQ_PATTERN = re.compile(r"^[1-9][0-9]{4,19}$")
# 请求站点接口与短链时的浏览器标识
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


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


def _host_of(value: str) -> str | None:
    """URL 主机名（小写）；解析不了返回 None。"""
    try:
        return (urlsplit(value).hostname or "").lower() or None
    except ValueError:
        return None


def _qq_from_url_query(value: str) -> str | None:
    """从唤醒链接的 query 里取目标号（``uin=`` 或 ``kfuin=``），取不到返回 None。"""
    try:
        query = parse_qs(urlsplit(value).query)
    except ValueError:
        return None
    for key in _WAKE_QQ_QUERY_KEYS:
        for candidate in query.get(key) or []:
            candidate = candidate.strip()
            if _QQ_PATTERN.match(candidate):
                return candidate
    return None


def _qq_from_short_link(value: str) -> str | None:
    """``wpa1.qq.com/<短码>`` 短链：跟随 302 落到 qidian 落地页后从 query 取 ``kfuin``。"""
    if _host_of(value) not in _WPA_SHORT_LINK_HOSTS:
        return None
    try:
        response = httpx.get(
            value, headers=_BROWSER_HEADERS, follow_redirects=True, timeout=QQ_RESOLVE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # 短链过期 / 网络不通都只意味「这条兜底路走不通」
        logger.warning("qidian.resolve.short_link_failed url=%s error=%s", value[:120], exc)
        return None
    resolved = _qq_from_url_query(str(response.url))
    if not resolved:
        logger.warning("qidian.resolve.short_link_empty url=%s final=%s", value[:80], str(response.url)[:160])
    return resolved


def _qq_from_value(value: str) -> str | None:
    """把「``QQ`` 参数值 / 接口返回的 ``Data``」换算成 QQ 号。

    覆盖真实链接的三种形态：明文号直接返回；唤醒链接取 ``uin=`` / ``kfuin=``；
    ``wpa1.qq.com`` 短链跟随 302 后再取。都不是就返回 None。
    """
    candidate = unquote((value or "").strip())
    if not candidate:
        return None
    if _QQ_PATTERN.match(candidate):
        return candidate
    if not _host_of(candidate):
        return None
    return _qq_from_url_query(candidate) or _qq_from_short_link(candidate)


def extract_target_qq(link: str) -> str | None:
    """链接自身 ``QQ`` 参数（兼容小写 ``qq``）里的目标号；拿不到返回 None。

    「``QQ`` 参数本身就是唤醒链接」的真实形态（``wpa.qq.com/msgrd?...&uin=`` 与
    ``wpa1.qq.com`` 短链）就是靠这里取号——站点对这种链接直接 302，不调接口。
    """
    params = _query_params(link)
    for key in ("QQ", "qq"):
        resolved = _qq_from_value(params.get(key) or "")
        if resolved:
            return resolved
    return None


def resolve_target_qq(link: str, provided: str | None = None) -> tuple[str | None, str]:
    """确定目标 QQ 号，返回 ``(QQ 号, 来源)``；都拿不到返回 ``(None, "")``。

    ``provided``（调用方传入的 ``targetQqNumber``）优先且直接生效，不再访问链接或站点接口；
    只有它为 None/空白时才走下面的链接解析兜底。来源取 ``caller`` / ``link`` / ``api``。

    没有 ``provided`` 时的兜底顺序：先取链接自身的 ``QQ`` 参数（明文号直接可用，
    ``wpa.qq.com/msgrd?...&uin=`` 取 ``uin``），再退回站点自己的 ``GetQQLinkUrl``
    （GoClient 页面 JS 用的就是它，返回的是同一条唤醒链接）。

    注意这两条兜底路都可能拿到**企点路由/客服号**而不是接待人号：``wpa1.qq.com`` 短链
    302 之后的 ``kfuin``、以及 ``GetQQLinkUrl`` 返回的 ``kfuin`` 都实测与 QQ 客户端里显示
    的接待人 QQ 号不相等（2026-10-15 真机：短链目标 3009004623，识别到的接待人是
    800885759）。所以客户端能从华强结果页读到商家 QQ 号时，一定要通过 ``provided``
    直接传进来；链接解析只用于没有该号的老链路。
    """
    caller = (provided or "").strip()
    if caller:
        if not _QQ_PATTERN.match(caller):
            raise QidianError("QQ_QIDIAN_QQ_UNRESOLVED",
                              f"调用方提供的目标 QQ 号格式不合法：{caller[:32]!r}")
        logger.info("qidian.resolve source=caller qq=%s", caller)
        return caller, "caller"

    direct = extract_target_qq(link)
    if direct:
        logger.info("qidian.resolve source=link qq=%s", direct)
        return direct, "link"

    payload = _query_params(link)
    for key in QIDIAN_API_PARAM_KEYS:
        payload.setdefault(key, "")
    try:
        response = httpx.post(
            QIDIAN_QQ_API,
            json=payload,
            headers={**_BROWSER_HEADERS, "Referer": link, "X-Requested-With": "XMLHttpRequest"},
            timeout=QQ_RESOLVE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body: Any = response.json()
    except Exception as exc:  # 网络/HTTP/JSON 任何问题都只意味「接口这条路走不通」
        logger.warning("qidian.resolve.api_failed error=%s", exc)
        return None, ""

    data = body.get("Data") if isinstance(body, dict) else None
    resolved = _qq_from_value(data) if isinstance(data, str) else None
    if resolved:
        logger.info("qidian.resolve source=api qq=%s", resolved)
        return resolved, "api"
    logger.info(
        "qidian.resolve.api_empty error=%s message=%s",
        body.get("Error") if isinstance(body, dict) else None,
        body.get("Message") if isinstance(body, dict) else None,
    )
    return None, ""


def _pane_signature(pane) -> tuple:
    """名单指纹（行名 + 屏幕矩形）：与 contact_lookup 的口径一致。"""
    return tuple((row.name, row.rect) for row in pane.rows)


def _read_baseline(hwnd: int) -> tuple[tuple | None, str | None]:
    """唤醒前的「会话名单指纹 + 激活行名」；读不到返回 ``(None, None)``。

    只是给「链接是否真的换了会话」留一个对照点，读不到不算错误，也不阻断唤醒。
    """
    from .contact_lookup import read_pane

    try:
        pane = read_pane(hwnd)
    except Exception as exc:  # 树重建/不可用等任何异常都不阻断唤醒
        logger.info("qidian.wake.baseline_unavailable error=%s", exc)
        return None, None
    rows = pane.rows
    signature = tuple(row.name for row in rows) if rows else None
    selected = pane.selected_rows()
    active = selected[0].name if len(selected) == 1 else None
    return signature, active


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


def _wait_active_contact(
    hwnd: int, deadline: float, wake_started: float, baseline_signature: tuple | None,
) -> tuple[str, tuple | None]:
    """等到「链接生效后的一份稳定名单 + 唯一激活行」，返回 ``(激活行会话名, 名单行名指纹)``。

    唤醒不是「打开浏览器就生效」：浏览器要起页、GoClient 再 302/跳转、QQ 才把目标会话顶到
    最前，真机实测有数秒延迟，所以这里**先等名单相对唤醒前发生变化**（新会话出现或次序改变），
    再要求名单在 ``WAKE_SETTLE_SECONDS`` 内不再变化且有唯一激活行；一直没变化时最多等
    ``WAKE_CHANGE_BUDGET_SECONDS``，之后按「名单本来就稳定」返回，成败交上层生效判定。
    超时按最后观测到的状态报具体错误，不猜测接待人。
    """
    from .accessibility import AccessibilityUnavailableError
    from .contact_lookup import read_pane

    baseline = tuple(baseline_signature) if baseline_signature else None
    change_deadline = min(deadline, wake_started + WAKE_CHANGE_BUDGET_SECONDS)
    previous: tuple | None = None
    last_change_at = wake_started
    changed = False
    selected_count = 0
    while time.monotonic() < deadline:
        try:
            pane = read_pane(hwnd)
        except AccessibilityUnavailableError as exc:
            # 元素树重建期读不到：不算稳定，继续等到预算用尽
            logger.warning("qidian.wake.pane_unavailable error=%s", exc)
            previous = None
            time.sleep(WAKE_POLL_INTERVAL_SECONDS)
            continue

        names = tuple(row.name for row in pane.rows)
        selected = pane.selected_rows()
        selected_count = len(selected)
        now = time.monotonic()
        if pane.rows and names != previous:
            # 名单变了（首次读到也算一次变化）：重置 settle 计时
            last_change_at = now
            if baseline is not None and names != baseline:
                changed = True
        settled = bool(pane.rows) and now - last_change_at >= WAKE_SETTLE_SECONDS
        if settled and selected_count == 1 and (changed or now >= change_deadline):
            return selected[0].name, names
        previous = names
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


def wake_and_identify(link: str, target_qq_number: str | None = None, *,
                      timeout: float = WAKE_TIMEOUT_BUDGET_SECONDS) -> dict:
    """唤醒 qidian 链接对应的 QQ 会话并识别当前接待人。

    ``target_qq_number`` 是调用方（客户端从华强搜索结果页）读到的商家 QQ 号：给了就用它，
    链接解析整体跳过；没给才退回链接 ``QQ`` 参数与站点 ``GetQQLinkUrl`` 兜底。

    只在 Dispatcher 的 worker 线程内调用（串行消费队列），返回结果 dict；
    任何业务失败都抛 ``QidianError``（错误码 ``QQ_QIDIAN_*``）。
    """
    normalized = normalize_link(link)
    target_qq, target_source = resolve_target_qq(normalized, target_qq_number)
    if not target_qq:
        raise QidianError("QQ_QIDIAN_QQ_UNRESOLVED",
                          "调用方没有传目标 QQ 号，链接既没有可识别的 QQ 参数，qidian 接口也解析不出号")
    logger.info("qidian.wake.target qq=%s source=%s", target_qq, target_source)

    started = time.monotonic()
    deadline = started + timeout
    window = _ensure_qq_window(deadline)
    hwnd = window._hWnd
    logger.info("qidian.wake.window_ready hwnd=%s elapsed_ms=%.1f",
                hwnd, (time.monotonic() - started) * 1000)

    # 唤醒前的名单指纹与激活行：用来判定「这次链接到底有没有真的把会话换过去」
    baseline_signature, baseline_active = _read_baseline(hwnd)
    logger.info("qidian.wake.baseline active=%r rows=%s", baseline_active,
                len(baseline_signature) if baseline_signature else None)

    wake_started = time.monotonic()
    try:
        # 交给默认浏览器打开（与客户端 openExternal 同一条已验证路径）
        os.startfile(normalized)
    except OSError as exc:
        raise QidianError("QQ_QIDIAN_WAKE_FAILED", f"无法打开 qidian 链接：{exc}") from exc

    qq_number: str | None = None
    for attempt in range(1, IDENTIFY_ATTEMPTS + 1):
        contact_name, post_signature = _wait_active_contact(hwnd, deadline, wake_started, baseline_signature)
        logger.info("qidian.wake.session_stable contact=%r attempt=%d wait_ms=%.1f",
                    contact_name, attempt, (time.monotonic() - wake_started) * 1000)
        try:
            qq_number = _identify_active_qq()
            break
        except QidianError as exc:
            # 会话刚被顶到最前时名单还在重绘：色带判定会瞬时读不到激活行（或读到多行）。
            # 这属于瞬时态而不是业务失败——只要还有预算就重新等稳定再识别一次。
            if (exc.code not in _IDENTIFY_RETRY_CODES or attempt >= IDENTIFY_ATTEMPTS
                    or time.monotonic() >= deadline - IDENTIFY_MIN_REMAINING_SECONDS):
                raise
            logger.info("qidian.wake.identify_retry attempt=%d code=%s", attempt, exc.code)
    verified = qq_number is not None and qq_number == target_qq
    # 生效判定：QQ 号对上，或者会话名单确实变了（唤醒会把目标会话顶到最前 / 新增一行）
    # → 认定这次唤醒生效；只有「名单和唤醒前完全一样，且读到的 QQ 号与目标对不上」才说明
    # 链接没起作用（浏览器没唤起 QQ / 商家不允许临时会话），此时上面那份名单只是上一次
    # 留下的会话，绝不能用它写映射。
    list_changed = (
        baseline_signature is not None and post_signature is not None
        and tuple(baseline_signature) != tuple(post_signature)
    )
    if not verified and qq_number is not None and not list_changed:
        raise QidianError(
            "QQ_QIDIAN_WAKE_FAILED",
            f"链接未生效：会话名单与唤醒前完全一致（激活行 {contact_name!r}，识别 QQ 号 "
            f"{qq_number}，链接目标 {target_qq}），拒绝写入映射",
        )
    if not verified and not list_changed and baseline_active == contact_name:
        logger.warning("qidian.wake.unverified_unchanged contact=%r qq=%s target=%s",
                       contact_name, qq_number, target_qq)
    logger.info("qidian.wake.done contact=%r qq=%s target=%s source=%s verified=%s changed=%s total_ms=%.1f",
                contact_name, qq_number, target_qq, target_source, verified, list_changed,
                (time.monotonic() - started) * 1000)
    return {
        "linkHash": link_hash(normalized),
        "contactName": contact_name,
        "qqNumber": qq_number,
        "targetQqNumber": target_qq,
        "targetSource": target_source,
        "qqNumberVerified": verified,
        "activeStable": True,
        "wokeAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
