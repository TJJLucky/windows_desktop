"""Dispatcher：全局唯一的 QQ 单消费者任务队列。

为什么需要：
- QQ 桌面操作（视觉识别 + 模拟点击）不是线程安全的，多个 HTTP 请求并发操作会互相干扰
  （一个请求在滚动列表，另一个在点发送，窗口状态会乱）。
- 因此所有对 QQ 的操作必须串行：Dispatcher 用 FIFO 队列 + 后台工作线程，
  保证同一时刻只有一个操作在控制 QQ。

对外 API（get_contact_list / send_message / read_message_list）：
  入队 → 阻塞等待结果，统一返回 (result, error) 二元组；
  超时会取消尚未开始的任务；已经开始的任务继续完成，调用方必须按结果未知处理。
"""

# queue：FIFO 任务队列 + 结果队列
import queue
# threading：后台工作线程 + 停止事件
import threading
import time
import logging
from dataclasses import dataclass, field
# 类型标注
from typing import Any, Optional, Tuple

# 三个单例管理器：用户列表 / 输入框 / 消息列表
from .user_list import UserList
from .input import InputBox
from .message import MessageList


logger = logging.getLogger("qq_service.dispatcher")


class TaskTimeoutError(TimeoutError):
    def __init__(self, code: str, *, started: bool):
        super().__init__(code)
        self.started = started


@dataclass
class _Task:
    kind: str
    args: tuple
    deadline: float
    timeout_code: str
    result: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))
    lock: threading.Lock = field(default_factory=threading.Lock)
    started: bool = False
    cancelled: bool = False


class Dispatcher:
    """QQ 单消费者任务队列（单例）。

    用法：
        op = Dispatcher()
        # payload = {"contacts": 会话名 → 行信息, "complete": 是否覆盖整张名单}
        payload, exc = op.get_contact_list(timeout=60.0, all_contacts=False)
        ok, exc = op.send_message("DIsaac", "你好", timeout=60.0)
        messages, exc = op.read_message_list("DIsaac", timeout=60.0)
    """

    # 类级单例实例
    _instance: "Dispatcher | None" = None

    def __new__(cls):
        # 单例：全局只有一个 Dispatcher（保证全进程只有一个消费队列）
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # 已初始化过则跳过（单例 __init__ 会被重复调用）
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        # 三个业务单例：好友列表 / 输入框 / 消息列表
        self.userList = UserList()
        self.inputBox = InputBox()
        self.messageList = MessageList()

        # 任务队列：开始与取消共用任务锁，避免超时后才执行桌面操作
        self.task_queue: queue.Queue = queue.Queue()
        # 后台工作线程
        self.worker_thread: Optional[threading.Thread] = None
        # 停止信号（stop() 时置位）
        self.stop_event = threading.Event()
        # 启动后台工作线程
        self.start_worker()

    @property
    def pending_task_count(self) -> int:
        """待执行任务数量（调用方可用于观察队列积压）"""
        return self.task_queue.qsize()

    def start_worker(self):
        """启动后台串行工作线程（已启动则跳过）。"""
        # 线程已存在且存活 → 不重复启动
        if self.worker_thread is not None and self.worker_thread.is_alive():
            return
        # 清除停止信号（支持 stop 后重新 start）
        self.stop_event.clear()
        # daemon=True：主进程退出时线程自动结束（不阻塞程序退出）
        self.worker_thread = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker_thread.start()

    def worker_loop(self):
        """工作线程循环：逐个执行UI任务（串行消费队列）。"""
        # 循环直到收到停止信号
        while not self.stop_event.is_set():
            try:
                # 取任务：0.5s 超时（配合 stop_event 快速退出）
                task = self.task_queue.get(timeout=0.5)
            except queue.Empty:
                # 队列空 → 继续轮询停止信号
                continue

            with task.lock:
                if task.cancelled or time.monotonic() >= task.deadline:
                    task.cancelled = True
                    task.result.put((None, TaskTimeoutError(task.timeout_code, started=False)))
                    self.task_queue.task_done()
                    logger.info("queue.task.expired kind=%s started=false", task.kind)
                    continue
                task.started = True
            task_type, args, res_q = task.kind, task.args, task.result
            result: Any = None
            exc: Optional[Exception] = None
            from .template_runtime import operation_scope
            try:
                with operation_scope():
                    # 全部在 worker 线程串行执行，固定本次操作的模板版本。
                    if task_type == "send_message":
                        contact_name, text = args
                        result = self.send_message_impl(contact_name, text)
                    elif task_type == "read_message_list":
                        contact_name, = args
                        result = self.read_message_list_impl(contact_name)
                    elif task_type == "get_contact_list":
                        result = self.get_contact_list_impl(*args)
                    elif task_type == "identify_current_contact":
                        from .contact_number import ContactNumberReader
                        number = ContactNumberReader().identify_current()
                        result = ({"qqNumber": number} if number is not None else
                                  {"qqNumber": None, "skipped": True, "reason": "QQ_CONTACT_NUMBER_COPY_UNAVAILABLE"})
                    elif task_type == "qidian_wake":
                        link, target_qq_number, metadata, mappings = args
                        result = self.wake_qidian_impl(link, target_qq_number, metadata, mappings)
                    elif task_type == "check_capture_ready":
                        result = self.check_capture_ready_impl()
                    else:
                        raise ValueError(f"unknown task {task_type}")
            except Exception as e:
                # 捕获实现抛出的任何异常
                exc = e
            finally:
                # 把结果/异常放回给调用方（调用方在 res_q.get 处收到）
                res_q.put((result, exc))
                # 标记任务完成（join 语义用）
                self.task_queue.task_done()

    def stop(self):
        """停止工作线程，程序退出调用。"""
        # 置位停止信号（worker 会在下一次循环检查退出）
        self.stop_event.set()
        # 当前业务完成并退出后才能释放它正在使用的采集资源。
        if self.worker_thread:
            self.worker_thread.join()

        # ========= 对外API：全部入队列，阻塞等待 =========

    def _submit(self, kind: str, args: tuple, timeout: float, code: str, empty):
        task = _Task(kind, args, time.monotonic() + max(0, timeout), code)
        self.task_queue.put(task)
        try:
            return task.result.get(timeout=max(0, task.deadline - time.monotonic()))
        except queue.Empty:
            with task.lock:
                # 完成可能刚好发生在等待超时边界，优先返回真实结果。
                try:
                    return task.result.get_nowait()
                except queue.Empty:
                    if not task.started:
                        task.cancelled = True
                    logger.warning("queue.task.timeout kind=%s started=%s", kind, task.started)
                    return empty, TaskTimeoutError(code, started=task.started)

    def identify_current_contact(self, timeout: float = 30.0) -> Tuple[dict, Optional[Exception]]:
        return self._submit("identify_current_contact", (), timeout, "QQ_CONTACT_NUMBER_QUEUE_TIMEOUT", {})

    def wake_qidian(self, link: str, target_qq_number: str,
                    timeout: float = 45.0, *, metadata: dict, mappings) -> Tuple[dict, Optional[Exception]]:
        """整段唤醒与核号串行执行；尚未开始的过期任务不再打开链接。"""
        return self._submit("qidian_wake", (link, target_qq_number, metadata, mappings),
                            timeout, "QQ_QIDIAN_QUEUE_TIMEOUT", {})

    def check_capture_ready(self, timeout: float = 30.0) -> Tuple[dict, Optional[Exception]]:
        return self._submit("check_capture_ready", (), timeout, "QQ_CAPTURE_QUEUE_TIMEOUT", {})

    def get_contact_list(self, timeout: float = 120.0, contact_name: str | None = None,
                         all_contacts: bool = False) -> Tuple[dict, Optional[Exception]]:
        return self._submit("get_contact_list", (contact_name, all_contacts),
                            timeout, "QQ_CONTACTS_QUEUE_TIMEOUT", {})

    def send_message(self, contact_name: str, text: str, timeout: float = 120.0) -> Tuple[bool, Optional[Exception]]:
        return self._submit("send_message", (contact_name, text), timeout, "QQ_SEND_QUEUE_TIMEOUT", False)

    def read_message_list(self, contact_name: str, timeout: float = 120.0) -> Tuple[dict, Optional[Exception]]:
        return self._submit("read_message_list", (contact_name,), timeout, "QQ_MESSAGES_QUEUE_TIMEOUT", {})

    # ========= 内部真正实现（在worker线程运行） =========
    def check_capture_ready_impl(self) -> dict:
        """实际执行窗口就绪检查；仅在 Dispatcher worker 中调用。"""
        from .window_ops import ensure_qq_window_with_retry

        win = ensure_qq_window_with_retry()
        return {"ready": True, "windowTitle": getattr(win, "title", None)}

    def wake_qidian_impl(self, link: str, target_qq_number: str, metadata: dict, mappings) -> dict:
        """实际实现：qidian 链接唤醒 + 会话稳定等待 + 当前接待人识别。"""
        from .qidian import resolve_verified

        return resolve_verified(link, target_qq_number, metadata, mappings)

    def get_contact_list_impl(self, contact_name: str | None = None,
                              all_contacts: bool = False) -> dict:
        """实际实现：读会话名单（无障碍树，昵称 → 行信息）。

        返回 ``{"contacts": ..., "complete": ...}``；all_contacts=True 时滚动扫掠整张名单。
        """
        return self.userList.get_user_list(contact_name, all_contacts)

    def send_message_impl(self, contact_name: str, text: str) -> bool:
        """实际实现：输入文本（内部自动激活会话）→ 快捷键发送。"""
        # 发送方法内部自动激活用户（点击列表行切换会话，已激活跳过）→ 聚焦输入框 → 粘贴 → Enter 发送
        self.inputBox.send_text(contact_name, text)
        return True

    def read_message_list_impl(self, contact_name: str) -> dict:
        """委托 MessageList 完成激活、框选复制和解析。"""
        return self.messageList.read_messages(contact_name)
