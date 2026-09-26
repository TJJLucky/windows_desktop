"""把既有 QQ 实现层收敛为 Service 内部业务端口。

职责：
- 定义 QqAutomationPort 抽象（接口契约），Service 层只依赖该端口，测试可注入替身；
- 提供 LegacyQqAutomationFacade 真实实现：复用既有 Dispatcher 单消费者队列，不改变
  WGC 截图、OCR、模板匹配或输入模拟等底层实现（它们仍从 utils 包按原样使用）；
- 把 Dispatcher 的 (result, error) 元组约定翻译为 Service 层的异常语义（QqAutomationError），
  让 HTTP 层能统一投影为 503。
"""

# 延迟求值类型注解
from __future__ import annotations

# Protocol：结构化类型协议（鸭子类型接口），无需继承即可满足
from typing import Protocol


class QqAutomationError(RuntimeError):
    """可安全投影为公开 API 错误的 QQ 自动化失败。

    Service 层捕获它后统一返回 503；message 里的错误码（如 QQ_CONTACTS_UNAVAILABLE）
    会原样传给调用方，便于前端定位失败阶段。
    """


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
    """复用既有 Dispatcher FIFO，不改变 WGC、OCR 或输入实现。"""

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
            raise QqAutomationError("QQ_SEND_FAILED") from error
        # 规范化的成功结果：ok/sent 同值、消息长度、无错误
        return {"ok": bool(sent), "sent": bool(sent), "textLength": len(text), "error": None}

    def read_messages(self, contact_name: str) -> dict:
        """读取可见消息：入队执行（最长等 60s），翻译成 Service 层消息结构。"""
        # 入队读取；error 非空表示失败
        messages, error = self._operator().read_message_list(contact_name, timeout=60.0)
        if error is not None:
            raise QqAutomationError("QQ_MESSAGES_UNAVAILABLE") from error
        # 把 utils 层的 Message dict（text/is_self/rect）翻译为 HTTP 契约结构：
        # 矩形拆成 x/y/w/h 四个字段（前端更好消费），is_self 保留（用于判断 in/out）
        payload = [
            {
                "text": item["text"],
                "isSelf": item["is_self"],
                "x": item["rect"][0],
                "y": item["rect"][1],
                "w": item["rect"][2],
                "h": item["rect"][3],
            }
            for item in messages
        ]
        return {"ok": True, "messages": payload, "count": len(payload), "error": None}

    def check_capture_ready(self) -> dict:
        """检查 QQ 截图是否可用：调用 ensure_qq_window_with_retry 做窗口就绪检测。

        与业务操作（读/发）不同，这是探测接口：窗口不可用返回 ready=False，
        不抛 QqAutomationError（HTTP 层 200 + ready=false，而非 503）。
        """
        # 延迟 import：避免在非 Windows/无桌面环境加载窗口检测重依赖
        from utils.qq.window_ops import ensure_qq_window_with_retry

        try:
            # 分级校验 + 重试：窗口存在 → 用户未动时快照复用 → 仅变化时置顶/最大化/试截
            win = ensure_qq_window_with_retry()
            # 就绪：报告窗口标题（辅助确认窗口身份）
            return {"ready": True, "windowTitle": getattr(win, "title", None)}
        except Exception as exc:
            # 重试耗尽仍未就绪：返回不可用 + 错误码（不抛出，保持探测语义）
            print(f"[WARN] 截图可用性检测失败: {exc}")
            return {"ready": False, "error": "QQ_WINDOW_NOT_READY"}
