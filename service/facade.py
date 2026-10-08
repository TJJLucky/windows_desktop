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
    """请求的联系人不在当前可见 QQ 用户列表中。"""

    def __init__(self, contact_name: str) -> None:
        self.contact_name = contact_name
        super().__init__(f"QQ_CONTACT_NOT_FOUND: 当前可见 QQ 用户列表中未找到联系人「{contact_name}」")


def _raise_operation_error(error: Exception, contact_name: str, fallback_code: str) -> None:
    """把底层的联系人缺失异常保留为 HTTP 可辨识的业务错误。"""
    # 延迟导入：保持 facade 在没有 Windows GUI 依赖的测试环境中仍可加载。
    from utils.qq.user_list import ContactNotFoundError

    if isinstance(error, ContactNotFoundError):
        raise QqContactNotFoundError(contact_name) from error
    raise QqAutomationError(fallback_code) from error


class QqAutomationPort(Protocol):
    """QQ 自动化端口抽象：Service 层与具体实现解耦的接口约定。

    三个操作全部是阻塞式（视觉识别 + 模拟点击），HTTP 层用 asyncio.to_thread 放进线程池。
    """

    def list_contacts(self) -> dict[str, dict]:
        """读取当前可见联系人列表，返回 联系人名 → 信息 dict。"""
        ...

    def send_message(self, contact_name: str, text: str) -> dict:
        """向指定联系人发送文本，返回结果 dict。"""
        ...

    def read_messages(self, contact_name: str) -> dict:
        """读取指定联系人的可见消息，返回含 messages 列表的 dict。"""
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

        return Dispatcher()

    def list_contacts(self) -> dict[str, dict]:
        """读联系人列表：入队执行（最长等 60s），失败抛 QqAutomationError。"""
        # Dispatcher 返回 (结果, 异常) 二元组：error 非空表示失败
        contacts, error = self._operator().get_contact_list(timeout=60.0)
        if error is not None:
            # 失败统一投影为业务错误（HTTP 层转 503），错误码带出失败阶段
            raise QqAutomationError("QQ_CONTACTS_UNAVAILABLE") from error
        return contacts

    def send_message(self, contact_name: str, text: str) -> dict:
        """发送消息：入队执行（最长等 60s），成功返回标准结果结构。"""
        # 入队发送；error 非空表示失败（如窗口不可用、识别失败）
        sent, error = self._operator().send_message(contact_name, text, timeout=60.0)
        if error is not None:
            _raise_operation_error(error, contact_name, "QQ_SEND_FAILED")
        # 规范化的成功结果：ok/sent 同值、消息长度、无错误
        return {"ok": bool(sent), "sent": bool(sent), "textLength": len(text), "error": None}

    def read_messages(self, contact_name: str) -> dict:
        """读取可见消息：入队执行（最长等 60s），翻译成 Service 层消息结构。"""
        # 入队读取；error 非空表示失败
        result, error = self._operator().read_message_list(contact_name, timeout=60.0)
        if error is not None:
            _raise_operation_error(error, contact_name, "QQ_MESSAGES_UNAVAILABLE")
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
