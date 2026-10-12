"""把既有 QQ 实现层收敛为 Service 内部业务端口。

职责：
- 定义 QqAutomationPort 抽象（接口契约），Service 层只依赖该端口，测试可注入替身；
- 提供 LegacyQqAutomationFacade 真实实现：复用既有 Dispatcher 单消费者队列，不改变
  QQ 窗口定位、模板匹配、原生复制或输入模拟等底层实现；
- 把 Dispatcher 的 (result, error) 元组约定翻译为 Service 层的异常语义（QqAutomationError），
  让 HTTP 层能统一投影为 503。
"""

# 延迟求值类型注解
from __future__ import annotations

import logging

# Protocol：结构化类型协议（鸭子类型接口），无需继承即可满足
from typing import Protocol

logger = logging.getLogger("qq_service.facade")


class QqAutomationError(RuntimeError):
    """可安全投影为公开 API 错误的 QQ 自动化失败。

    Service 层捕获它后统一返回 503；message 里的错误码（如 QQ_CONTACTS_UNAVAILABLE）
    会原样传给调用方，便于前端定位失败阶段。
    """


class QqContactNotFoundError(QqAutomationError):
    """滚动查找后仍没有请求的联系人。"""

    def __init__(self, qq_number: str) -> None:
        self.qq_number = qq_number
        super().__init__(f"QQ_CONTACT_NOT_FOUND: QQ 用户列表滚动查找后未找到 QQ 号「{qq_number}」")


_ERROR_CODE_PREFIXES = (
    "QQ_CONTACT_SCAN_LIMIT",
    "QQ_CONTACT_SCAN_CHANGED",
    "QQ_CONTACT_NOT_FOUND",
    "QQ_CONTACT_NUMBER_INVALID",
    "QQ_CONTACT_NUMBER_ACTIVE_NOT_FOUND",
    "QQ_CONTACT_NUMBER_ACTIVE_AMBIGUOUS",
    "QQ_CONTACT_NUMBER_ROW_CHANGED",
    "QQ_CONTACT_NUMBER_MENU_CHANGED",
    "QQ_CONTACT_NUMBER_WINDOW_CHANGED",
    "QQ_CONTACT_NUMBER_TEMPLATE_UNAVAILABLE",
    "QQ_CONTACT_NUMBER_COPY_UNAVAILABLE",
    "QQ_USER_LIST_REGION_NOT_FOUND",
    "QQ_MESSAGE_REGION_NOT_FOUND",
    "QQ_INPUT_REGION_NOT_FOUND",
    "QQ_COPY_ACTION_NOT_FOUND",
    "QQ_SELECTION_NOT_ENTERED",
    "QQ_COPY_CLIPBOARD_UNCHANGED",
    "QQ_COPY_TEXT_EMPTY",
    "QQ_COPY_PARSE_FAILED",
    "QQ_COPY_CAPTURE_FAILED",
    "QQ_MESSAGE_REGION_TOO_SMALL",
    "QQ_WINDOW_NOT_READY",
)


def _operation_error_code(error: Exception, fallback_code: str) -> str:
    """从底层异常提取稳定的公开错误码，无法识别时使用阶段兜底码。"""
    detail = str(error).strip()
    if error.__class__.__name__ == "QQWindowNotReadyError":
        return "QQ_WINDOW_NOT_READY"
    if "get_message_box_region_and_image" in detail:
        return "QQ_MESSAGE_REGION_NOT_FOUND"
    if "get_inputbox_region_and_image" in detail:
        return "QQ_INPUT_REGION_NOT_FOUND"
    if "get_userList_region_and_image" in detail:
        return "QQ_USER_LIST_REGION_NOT_FOUND"
    for code in _ERROR_CODE_PREFIXES:
        if code in detail:
            return code
    if detail.startswith("QQ_") and " " not in detail:
        return detail
    return fallback_code


def _raise_operation_error(error: Exception, qq_number: str, fallback_code: str) -> None:
    """把底层的联系人缺失异常保留为 HTTP 可辨识的业务错误。"""
    # 延迟导入：保持 facade 在没有 Windows GUI 依赖的测试环境中仍可加载。
    from utils.qq.contact_scan import ContactNotFoundError

    if isinstance(error, ContactNotFoundError):
        raise QqContactNotFoundError(qq_number) from error
    raise QqAutomationError(_operation_error_code(error, fallback_code)) from error


class QqAutomationPort(Protocol):
    """QQ 自动化端口抽象：Service 层与具体实现解耦的接口约定。

    三个操作全部是阻塞式（视觉识别 + 模拟点击），HTTP 层用 asyncio.to_thread 放进线程池。
    """

    def identify_current_contact(self) -> dict:
        """复制当前激活行的 QQ 号。"""
        ...

    def list_contacts(self, qq_number: str | None = None) -> dict[str, dict]:
        """读取当前可见联系人列表，返回 QQ 号 → 信息 dict。"""
        ...

    def send_message(self, qq_number: str, text: str) -> dict:
        """向指定 QQ 号发送文本，返回结果 dict。"""
        ...

    def read_messages(self, qq_number: str) -> dict:
        """读取指定 QQ 号的可见消息，返回含 messages 列表的 dict。"""
        ...

    def check_capture_ready(self) -> dict:
        """检查 QQ 截图能力（窗口就绪），返回 {ready, windowTitle?, error?}。

        探测语义：窗口不可用是有效检测结果（ready=False + error），不抛异常。
        """
        ...


class LegacyQqAutomationFacade:
    """复用既有 Dispatcher FIFO；消息读取使用 QQ 原生复制，不再使用气泡 OCR。"""

    def _operator(self):
        """惰性创建 Dispatcher 单例：延迟 import 避免在非 Windows 平台/无桌面环境加载重依赖。

        Dispatcher 是全局唯一的 QQ 消费入口（内部单例 + daemon worker + FIFO 队列），
        保证所有视觉操作串行执行、互不并发。
        """
        from utils.qq.dispatcher import Dispatcher
        if getattr(self, "layout_store", None) is not None:
            from utils.qq.template_runtime import configure
            configure(self.layout_store)

        self._dispatcher = Dispatcher()
        return self._dispatcher

    def close(self):
        """Stop the business consumer before closing native capture; keep GUI imports lazy."""
        dispatcher = getattr(self, "_dispatcher", None)
        if dispatcher is not None:
            dispatcher.stop()
            from utils.core.screenshot import WGCCapture
            WGCCapture.release()
            self._dispatcher = None

    def identify_current_contact(self) -> dict:
        result, error = self._operator().identify_current_contact(timeout=30.0)
        if error is not None:
            raise QqAutomationError(_operation_error_code(error, "QQ_CONTACT_NUMBER_IDENTIFY_FAILED")) from error
        return result

    def list_contacts(self, qq_number: str | None = None) -> dict[str, dict]:
        """读联系人列表：入队执行（最长等 60s），失败抛 QqAutomationError。"""
        # Dispatcher 返回 (结果, 异常) 二元组：error 非空表示失败
        contacts, error = (self._operator().get_contact_list(timeout=120.0, qq_number=qq_number)
                           if qq_number else self._operator().get_contact_list(timeout=120.0))
        if error is not None:
            # 失败统一投影为业务错误（HTTP 层转 503），错误码带出失败阶段
            _raise_operation_error(error, qq_number or "", "QQ_CONTACTS_UNAVAILABLE")
        return contacts

    def send_message(self, qq_number: str, text: str) -> dict:
        """发送消息：入队执行（最长等 60s），成功返回标准结果结构。"""
        # 入队发送；error 非空表示失败（如窗口不可用、识别失败）
        sent, error = self._operator().send_message(qq_number, text, timeout=60.0)
        if error is not None:
            _raise_operation_error(error, qq_number, "QQ_SEND_FAILED")
        # 规范化的成功结果：ok/sent 同值、消息长度、无错误
        return {"ok": bool(sent), "sent": bool(sent), "textLength": len(text), "error": None}

    def read_messages(self, qq_number: str) -> dict:
        """读取可见消息：入队执行（最长等 60s），翻译成 Service 层消息结构。"""
        # 入队读取；error 非空表示失败
        result, error = self._operator().read_message_list(qq_number, timeout=60.0)
        if error is not None:
            _raise_operation_error(error, qq_number, "QQ_MESSAGES_UNAVAILABLE")
        # 把 QQ 原生复制解析结果翻译为 HTTP 契约结构。
        # sender/timestamp/text/rawText 是权威字段，消息归属由外部智能体按 sender 判断。
        payload = [
            {
                "text": item["text"],
                "sender": item.get("sender"),
                "timestamp": item.get("timestamp"),
                "rawText": item.get("rawText"),
            }
            for item in result["messages"]
        ]
        return {
            "ok": True,
            "messages": payload,
            "count": len(payload),
            "copiedText": result["copiedText"],
            "error": None,
        }

    def check_capture_ready(self) -> dict:
        """检查 QQ 截图是否可用：调用 ensure_qq_window_with_retry 做窗口就绪检测。

        与业务操作（读/发）不同，这是探测接口：窗口不可用返回 ready=False，
        不抛 QqAutomationError（HTTP 层 200 + ready=false，而非 503）。
        """
        # capture 检查也必须经过 Dispatcher，避免与联系人 OCR、读消息、发送并发操作 QQ，
        # 同时消除多个请求首次并发延迟导入 utils.qq 子模块时的模块锁死锁。
        result, error = self._operator().check_capture_ready(timeout=30.0)
        if error is not None:
            logger.warning("capture.check.failed error=%s", error)
            return {"ready": False, "error": "QQ_WINDOW_NOT_READY"}
        return result
