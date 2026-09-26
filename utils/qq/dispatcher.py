"""Dispatcher：全局唯一的 QQ 单消费者任务队列。

为什么需要：
- QQ 桌面操作（视觉识别 + 模拟点击）不是线程安全的，多个 HTTP 请求并发操作会互相干扰
  （一个请求在滚动列表，另一个在点发送，窗口状态会乱）。
- 因此所有对 QQ 的操作必须串行：Dispatcher 用 FIFO 队列 + 后台工作线程，
  保证同一时刻只有一个操作在控制 QQ。

对外 API（get_contact_list / send_message / read_message_list）：
  入队 → 阻塞等待结果，统一返回 (result, error) 二元组；
  超时仅表示"没等到结果"，不保证任务未执行（任务可能仍在队列/执行中）。
"""

# queue：FIFO 任务队列 + 结果队列
import queue
# threading：后台工作线程 + 停止事件
import threading
# 类型标注
from typing import Any, Optional, Tuple

# 三个单例管理器：用户列表 / 输入框 / 消息列表
from .user_list import UserList
from .input import InputBox
from .message import MessageList


class Dispatcher:
    """QQ 单消费者任务队列（单例）。

    用法：
        op = Dispatcher()
        contacts, exc = op.get_contact_list(timeout=60.0)
        ok, exc = op.send_message("联系人", "你好", timeout=60.0)
        messages, exc = op.read_message_list("联系人", timeout=60.0)
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

        # 任务队列: 每个元素 (task_type, args, kwargs, result_queue)
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

            # 解包任务：类型 / 位置参数 / 关键字参数 / 结果队列
            task_type, args, kwargs, res_q = task
            result: Any = None
            exc: Optional[Exception] = None
            try:
                # 按任务类型分发到对应实现（全部在 worker 线程串行执行）
                if task_type == "send_message":
                    contact_name, text = args
                    result = self.send_message_impl(contact_name, text)
                elif task_type == "read_message_list":
                    contact_name, = args
                    result = self.read_message_list_impl(contact_name)
                elif task_type == "get_contact_list":
                    result = self.get_contact_list_impl()
                else:
                    # 未知任务类型 → 作为异常返回给调用方
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
        # 等待线程退出（最多 3 秒）
        if self.worker_thread:
            self.worker_thread.join(timeout=3)

        # ========= 对外API：全部入队列，阻塞等待 =========

    def get_contact_list(self, timeout: float = 120.0) -> Tuple[dict[str, dict], Optional[Exception]]:
        """获取联系人列表，入队列串行执行，缓存过期会执行截图解析。"""
        # 结果队列（容量 1：只放一个 (result, exc)）
        res_q = queue.Queue(maxsize=1)
        # 入队
        self.task_queue.put(("get_contact_list", (), {}, res_q))
        try:
            # 阻塞等待结果（最长 timeout 秒）
            return res_q.get(timeout=timeout)
        except queue.Empty:
            # 超时：返回空结果 + 超时异常（不保证任务未执行）
            return {}, TimeoutError("get_contact_list task timeout")

    def send_message(self, contact_name: str, text: str, timeout: float = 120.0) -> Tuple[bool, Optional[Exception]]:
        """提交发送任务到队列，阻塞等待执行完成
        返回 (成功bool, 异常对象 or None)
        """
        # 结果队列
        res_q = queue.Queue(maxsize=1)
        # 入队（worker 串行执行）
        self.task_queue.put(("send_message", (contact_name, text), {}, res_q))
        try:
            # 阻塞等待
            return res_q.get(timeout=timeout)
        except queue.Empty:
            # 超时：返回失败 + 超时异常
            return False, TimeoutError("send_message task timeout")

    def read_message_list(self, contact_name: str, timeout: float = 120.0) -> Tuple[list[dict], Optional[Exception]]:
        """提交读取消息任务到队列，阻塞等待执行完成。"""
        # 结果队列
        res_q = queue.Queue(maxsize=1)
        # 入队
        self.task_queue.put(("read_message_list", (contact_name,), {}, res_q))
        try:
            # 阻塞等待
            return res_q.get(timeout=timeout)
        except queue.Empty:
            # 超时：返回空列表 + 超时异常
            return [], TimeoutError("read_message_list task timeout")

    # ========= 内部真正实现（在worker线程运行） =========
    def get_contact_list_impl(self) -> dict[str, dict]:
        """实际实现：读用户列表（内部 1s 缓存）。"""
        return self.userList.get_user_list()

    def send_message_impl(self, contact_name: str, text: str) -> bool:
        """实际实现：激活会话 → 输入文本 → 点击发送。"""
        # 首先根据名字激活用户（点击其行切换会话）
        self.userList.active_user_by_name(contact_name)
        # 聚焦输入框 → 粘贴文本 → 点击发送
        self.inputBox.send_text(text)
        # 发送后用户列表变化（未读红点可能消除），清缓存强制下次刷新
        self.userList.clear_user_list_cache()
        return True

    def read_message_list_impl(self, contact_name: str) -> list[dict]:
        """实际实现：激活会话 → 读取消息，翻译为对外结构。"""
        # 首先根据名字激活用户
        self.userList.active_user_by_name(contact_name)
        # 读取消息（自动截图 + 气泡检测 + OCR）
        messages = self.messageList.read_messages()
        # 读取后同样清缓存（会话切换会影响列表状态）
        self.userList.clear_user_list_cache()
        # 翻译为对外 dict 结构（text / is_self / rect）
        return [
            {
                "text": m.text,
                "is_self": m.is_self,
                "rect": list(m.rect),
            }
            for m in messages
        ]
