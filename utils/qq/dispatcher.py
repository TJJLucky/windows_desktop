import queue
import threading
from typing import Any, Optional, Tuple

from .user_list import UserList
from .input import InputBox
from .message import MessageList


class Dispatcher:
    _instance: "Dispatcher | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self.userList = UserList()
        self.inputBox = InputBox()
        self.messageList = MessageList()

        # 任务队列: 每个元素 (task_type, args, kwargs, result_queue)
        self.task_queue: queue.Queue = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        # 启动后台工作线程
        self.start_worker()

    @property
    def pending_task_count(self) -> int:
        """待执行任务数量"""
        return self.task_queue.qsize()
    
    def start_worker(self):
        """启动后台串行工作线程"""
        if self.worker_thread is not None and self.worker_thread.is_alive():
            return
        self.stop_event.clear()
        self.worker_thread = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker_thread.start()

    def worker_loop(self):
        """工作线程循环：逐个执行UI任务"""
        while not self.stop_event.is_set():
            try:
                task = self.task_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            task_type, args, kwargs, res_q = task
            result: Any = None
            exc: Optional[Exception] = None
            try:
                if task_type == "send_message":
                    contact_name, text = args
                    result = self.send_message_impl(contact_name, text)
                elif task_type == "read_message_list":
                    contact_name, = args
                    result = self.read_message_list_impl(contact_name)
                elif task_type == "get_contact_list":
                    result = self.get_contact_list_impl()
                else:
                    raise ValueError(f"unknown task {task_type}")
            except Exception as e:
                exc = e
            finally:
                # 把结果/异常放回给调用方
                res_q.put((result, exc))
                self.task_queue.task_done()

    def stop(self):
        """停止工作线程，程序退出调用"""
        self.stop_event.set()
        if self.worker_thread:
            self.worker_thread.join(timeout=3)

        # ========= 对外API：全部入队列，阻塞等待 =========

    def get_contact_list(self, timeout: float = 120.0) -> Tuple[dict[str, dict], Optional[Exception]]:
        """获取联系人列表，入队列串行执行，缓存过期会执行截图解析"""
        res_q = queue.Queue(maxsize=1)
        self.task_queue.put(("get_contact_list", (), {}, res_q))
        try:
            return res_q.get(timeout=timeout)
        except queue.Empty:
            return {}, TimeoutError("get_contact_list task timeout")

    def send_message(self, contact_name: str, text: str, timeout: float = 120.0) -> Tuple[bool, Optional[Exception]]:
        """提交发送任务到队列，阻塞等待执行完成
        返回 (成功bool, 异常对象 or None)
        """
        res_q = queue.Queue(maxsize=1)
        self.task_queue.put(("send_message", (contact_name, text), {}, res_q))
        try:
            return res_q.get(timeout=timeout)
        except queue.Empty:
            return False, TimeoutError("send_message task timeout")

    def read_message_list(self, contact_name: str, timeout: float = 120.0) -> Tuple[list[dict], Optional[Exception]]:
        """提交读取消息任务到队列，阻塞等待执行完成"""
        res_q = queue.Queue(maxsize=1)
        self.task_queue.put(("read_message_list", (contact_name,), {}, res_q))
        try:
            return res_q.get(timeout=timeout)
        except queue.Empty:
            return [], TimeoutError("read_message_list task timeout")

    # ========= 内部真正实现（在worker线程运行） =========
    def get_contact_list_impl(self) -> dict[str, dict]:
        return self.userList.get_user_list()

    def send_message_impl(self, contact_name: str, text: str) -> bool:
        # 首先根据名字激活用户
        self.userList.active_user_by_name(contact_name)
        self.inputBox.send_text(text)
        self.userList.clear_user_list_cache()
        return True

    def read_message_list_impl(self, contact_name: str) -> list[dict]:
        # 首先根据名字激活用户
        self.userList.active_user_by_name(contact_name)
        messages = self.messageList.read_messages()
        self.userList.clear_user_list_cache()
        return [
            {
                "text": m.text,
                "is_self": m.is_self,
                "rect": list(m.rect),
            }
            for m in messages
        ]
