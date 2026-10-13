"""QQ 会话列表的无障碍树（UI Automation）读取。

QQ NT 基于 Chromium，默认向 Windows 发布 UI Automation 无障碍树（本意是给读屏
软件用）。它把"界面上有什么"直接报出来，因此比截图+OCR 快一到两个数量级，也不受
字体、主题、DPI 缩放影响。真机实测（1280x1390 窗口、26 行）：

- 枚举一屏行：约 30ms（截图+逐行右键取号是 480ms/行，拼图 OCR 是 550~850ms/屏）；
- 树里一次给到"视口附近约一屏半"的行（26 行，其中 15 行完整可见），多出来的行
  坐标为负或超出列表区下沿 → 可据此判定"要先滚动才点得到"；
- 每行自带 CurrentBoundingRectangle（屏幕物理像素），不必再用"窗口左上角 + 截图
  内像素"换算，绕开了跨电脑 DPI 的偏移风险；
- 行 ClassName 带标记：recent-contact-item--selected 是当前打开的会话，
  recent-contact-item--top 是置顶。

聊天区也暴露了会话名与消息区矩形，本模块只用这两样（真机实测 1280x1390）：

- chat-header__contact-name 直接给出当前会话名，激活目标会话后用它复核，
  不必再对会话列表做 OCR 复核；
- chat-msg-area__vlist（Name="消息列表"）的容器矩形，作为消息区定位失败的兜底锚点。

消息正文按最终口径仍走 QQ 原生框选复制（见 utils/qq/message.py），不用无障碍树读正文；
两种读法的实测对比与取舍见 docs/13-accessibility-tree.md。

已知边界（调用方要兜住）：
- 只给名字，不含 QQ 号 —— 号码仍要靠右击菜单"复制 QQ 号"取得；
- 消息正文里的图片只有占位文字，拿不到本地文件路径（现网框选复制能给出 <img src>）；
- 长列表仍要滚动（树里只保留视口附近的行）；
- 依赖 QQ 暴露无障碍层，QQ 大版本更新可能改结构；窗口最小化时读不到。
  两种情况统一抛 AccessibilityUnavailableError，调用方据此回退到截图链路。
"""

from __future__ import annotations

# logging：记录连接/枚举失败原因
import logging
# threading：单例在多线程（uvicorn 线程池）下串行访问 COM
import threading
# time：枚举耗时统计与重试等待
import time
# dataclass：行/面板的只读数据结构
from dataclasses import dataclass

# comtypes：COM 指针与 COMError
import comtypes
# POINTER/cast：把 pywinauto 的 element 转成 IUIAutomationElement
from comtypes import POINTER, cast
# UIA：IUIAutomationClient 类型与属性/控件类型常量
import comtypes.gen.UIAutomationClient as UIA
# Application / IUIA 一律延迟导入（见下方 _uia_client）：导入即创建 COM 单例，
# 必须发生在真正使用它的线程里。


logger = logging.getLogger("qq_service.accessibility")

# 会话列表容器的 Name 属性（QQ 自报的本地化名字）
LIST_PANE_NAME = "会话列表"
# 行元素的 ClassName 关键字
ROW_CLASS_MARKER = "recent-contact-item"
# 右击菜单里的同名元素（ClassName 也含 recent-contact-item），必须排除
MENU_CLASS_MARKER = "menu"
# 行 ClassName 里表示"当前打开的会话"的标记
ROW_SELECTED_MARKER = "--selected"
# 行 ClassName 里表示"置顶"的标记
ROW_PINNED_MARKER = "--top"
# 行内"内容块"的 ClassName 关键字：会话行的头像/文本子元素都挂在它下面
ROW_CONTENT_CLASS_MARKER = "item__content"

# 消息区容器的 Name 属性（QQ 自报的本地化名字）
MESSAGE_AREA_NAME = "消息列表"
# 消息区容器的 ClassName 关键字（诊断/兜底定位用）
MESSAGE_AREA_CLASS_MARKER = "chat-msg-area__vlist"
# 单条消息根的 ClassName（QQ 每条消息一个同名根元素）
MESSAGE_CLASS_NAME = "message"
# 消息根下"时间戳栏"的 ClassName 关键字（只有每个时间分组的首条消息带）
MESSAGE_TIMESTAMP_CLASS_MARKER = "message__timestamp"
# 消息根下"发送人/头像"容器的 ClassName 关键字
MESSAGE_SENDER_CLASS_MARKER = "avatar-span"
# 消息根下"正文"容器的 ClassName 关键字
MESSAGE_CONTENT_CLASS_MARKER = "msg-content-container"
# 会话内"消息容器"的 ClassName 关键字（系统提示消息不含它）
MESSAGE_CONTAINER_CLASS_MARKER = "message-container"
# 系统提示消息（入群提示等灰条）的 ClassName 关键字
MESSAGE_TIP_CLASS_MARKER = "gray-tip"
# 当前会话名元素的 ClassName
CHAT_NAME_CLASS_MARKER = "chat-header__contact-name"

# UIA ScrollPattern 的"不改变该方向"取值（SetScrollPercent 的水平参数）
NO_SCROLL_PERCENT = -1.0
# 连接句柄的超时（秒）；超时通常是窗口不属于当前进程或 QQ 刚重启
CONNECT_TIMEOUT_SECONDS = 2.0
# 枚举失败（多为窗口捕获面/元素树瞬时重建）的重试次数与间隔
ENUMERATE_ATTEMPTS = 3
ENUMERATE_RETRY_WAIT_SECONDS = 0.05


class AccessibilityUnavailableError(RuntimeError):
    """无障碍树当前不可用：未暴露、窗口最小化、连接失效或结构变化。

    调用方（滚动查找/激活）据此回退到截图链路，不应把它当成业务失败。
    """

    code = "QQ_ACCESSIBILITY_UNAVAILABLE"


def is_transient_error(error: BaseException) -> bool:
    """判断异常是否为"元素树瞬时重建"这类可重连重试的错误。

    除自抛的业务错误与 COMError/OSError 外，comtypes 在元素树重建瞬间读取
    属性会抛出裸 ``ValueError: NULL COM pointer access``（并非 COMError），
    真机实测在窗口刚布局/重绘后必现，因此一并纳入重试。
    """
    if isinstance(error, (AccessibilityUnavailableError, comtypes.COMError, OSError)):
        return True
    # comtypes 的 NULL 指针访问：COM 事件线程重置元素树时的高频瞬态错误
    return isinstance(error, ValueError) and "COM pointer" in str(error)


# pywinauto 的 IUIAutomation 客户端是进程级单例，COM 单元归属于"创建它的那个
# 线程"：在 A 线程创建、B 线程使用时，COM 要把调用封送回 A 线程的单元，而 A 线程
# 通常正阻塞在 Dispatcher 的任务队列等待上 → 调用永久挂死（真机复现：服务主线程
# 导入本模块后，worker 线程首次读元素属性就卡死，120s 后才由队列超时兜底）。
# 因此 pywinauto 延迟到"第一次真正执行 UIA 调用"的线程里导入，并固定归属线程；
# 之后从别的线程调用立刻抛 AccessibilityUnavailableError（可回退、可重试），
# 而不是无声挂死。
_UIA_CLIENT_LOCK = threading.Lock()
_UIA_OWNER_THREAD: int | None = None


def _uia_client():
    """按需导入 pywinauto 的 UIA 客户端，并把归属线程固定为当前线程。

    返回 ``(Application, IUIA)``；非归属线程调用直接抛业务错误，不进入 COM。
    """
    global _UIA_OWNER_THREAD
    ident = threading.get_ident()
    if _UIA_OWNER_THREAD is None:
        with _UIA_CLIENT_LOCK:
            if _UIA_OWNER_THREAD is None:
                _UIA_OWNER_THREAD = ident
    elif _UIA_OWNER_THREAD != ident:
        raise AccessibilityUnavailableError(
            "UIA client belongs to thread {owner}, called from thread {current}".format(
                owner=_UIA_OWNER_THREAD, current=ident))
    # 延迟导入：导入即创建 IUIA 单例，必须发生在归属线程里
    from pywinauto.application import Application
    from pywinauto.uia_defines import IUIA
    return Application, IUIA


def _iuia():
    """当前线程的 IUIAutomation 客户端对象（首次调用时在本线程创建单例）。"""
    return _uia_client()[1]().iuia


@dataclass(frozen=True)
class AccessibilityRow:
    """无障碍树里的一行会话。坐标是屏幕物理像素（UI Automation 直接给出）。"""

    # 昵称：行内第一个文本子元素的 Name
    name: str
    # 行内全部文本：昵称 / 时间 / 消息预览 等，按树顺序
    texts: tuple[str, ...]
    # 行矩形 (left, top, right, bottom)，屏幕物理像素
    rect: tuple[int, int, int, int]
    # 是否为当前打开的会话（ClassName 带 --selected）
    selected: bool
    # 是否为置顶会话（ClassName 带 --top）
    pinned: bool
    # 该会话是否支持聊天区框选复制；False = QQ 没给复制项（"我的手机""QQ邮箱提醒"
    # 这类服务/系统会话，真机实测复制按钮呈斜杠禁用态、剪贴板不变）
    copyable: bool

    @property
    def center(self) -> tuple[int, int]:
        """行中心点（点击头像/行用）。"""
        left, top, right, bottom = self.rect
        return ((left + right) // 2, (top + bottom) // 2)

    @property
    def height(self) -> int:
        """行高（判断可滚动步长用）。"""
        return self.rect[3] - self.rect[1]

    def visible_in(self, pane: tuple[int, int, int, int]) -> bool:
        """整行落在列表区内才算可点击；滚出去或只露一半都不算。"""
        left, top, right, bottom = self.rect
        return (top >= pane[1] and bottom <= pane[3] and
                left >= pane[0] and right <= pane[2])


@dataclass(frozen=True)
class AccessibilityPane:
    """一次枚举的结果：列表区矩形 + 全部已渲染行（含视口外的行）。"""

    # 列表区矩形 (left, top, right, bottom)，屏幕物理像素
    rect: tuple[int, int, int, int]
    # 全部已渲染行，按树顺序（与界面上下顺序一致）
    rows: tuple[AccessibilityRow, ...]
    # 本次枚举耗时（毫秒），用于日志与验收
    elapsed_ms: float = 0.0

    def visible_rows(self) -> tuple[AccessibilityRow, ...]:
        """只保留完整落在列表区内的行（可安全点击的那些）。"""
        return tuple(row for row in self.rows if row.visible_in(self.rect))

    def selected_rows(self) -> tuple[AccessibilityRow, ...]:
        """当前打开的会话行；正常只有 0 或 1 行。"""
        return tuple(row for row in self.rows if row.selected)

    def rows_named(self, name: str) -> tuple[AccessibilityRow, ...]:
        """按昵称精确匹配的行（同名多行时全部返回，由调用方判歧义）。"""
        return tuple(row for row in self.rows if row.name == name)


@dataclass(frozen=True)
class AccessibilityScroll:
    """会话列表容器的 UIA 滚动状态（ScrollPattern 直读）。

    Chromium 的滚动容器都发布 ScrollPattern：``view_size_percent`` 是"视口高度占
    内容总高度的百分比"，因此内容总高度 = 列表高度 / (view_size/100)；把目标位置
    换算成百分比后用 SetScrollPercent 一步到位，不必估算滚轮步长（真机实测
    SetScrollPercent 约 2~9ms，重读一屏行约 40~65ms）。
    """

    # 视口高度占内容高度的百分比（100 表示内容不足一屏，无需滚动）
    view_size_percent: float
    # 当前垂直滚动位置（0~100）
    position_percent: float
    # 容器当前是否可垂直滚动
    scrollable: bool

    def content_height(self, viewport_height: float) -> float:
        """由视口百分比换算内容总高度（像素）。"""
        if self.view_size_percent <= 0:
            return float(viewport_height)
        return float(viewport_height) * 100.0 / min(100.0, self.view_size_percent)


def _row_copyable(iuia, element) -> bool:
    """判断一行会话在聊天区是否还留着框选复制（QQ 的复制项）。

    QQ 的服务/系统会话（真机实测："我的手机""QQ邮箱提醒"）行内"内容块"
    （ClassName 含 item__content）下直接挂着一个无类名、无名字的 Image —— 它们的头像是
    <img> 元素；普通好友/群的行内没有任何 Image 子元素（真机实测 26 行名单里只有这两行
    命中）。这几类会话在聊天区被 QQ 禁用了框选复制（进入多选态后复制按钮变斜杠禁用态、
    剪贴板不变），因此提前标注出来，由 contact_lookup 把它们筛出可用名单。

    只认"无类名、无名字、直属于内容块"的 Image：免打扰/未读角标这类图标是带类名的
    Group（或其子节点），不会误命中；判据是 DOM 结构，与 DPI、窗口尺寸无关。
    """
    children = element.FindAll(UIA.TreeScope_Children, iuia.CreateTrueCondition())
    for index in range(children.Length):
        child = children.GetElement(index)
        if ROW_CONTENT_CLASS_MARKER not in (child.CurrentClassName or ""):
            continue
        inner = child.FindAll(UIA.TreeScope_Children, iuia.CreateTrueCondition())
        for inner_index in range(inner.Length):
            grandchild = inner.GetElement(inner_index)
            if grandchild.CurrentControlType != UIA.UIA_ImageControlTypeId:
                continue
            if (grandchild.CurrentClassName or "") or (grandchild.CurrentName or ""):
                continue
            return False
    return True


def _element_rect(element) -> tuple[int, int, int, int]:
    """读取元素的屏幕矩形，兼容 comtypes 返回 struct 与裸 4 元 tuple 两种形态。

    comtypes 在部分元素（真机实测：消息元素）上把 ``tagRECT`` 解成普通 tuple，
    直接取 ``.left`` 会抛 AttributeError，因此统一在这里归一化。
    """
    rect = element.CurrentBoundingRectangle
    try:
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except AttributeError:
        return (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))


def _child_elements(iuia, element) -> list:
    """取直接子元素（按树顺序）。"""
    children = element.FindAll(UIA.TreeScope_Children, iuia.CreateTrueCondition())
    return [children.GetElement(index) for index in range(children.Length)]


def _text_names(iuia, element) -> list[str]:
    """按树顺序收集子树里非空 **Text 控件**名称。

    只取 Text 控件：QQ 会在父 Group 上重复同一句话（avatar-span 的 Group 与其中的
    Text 同名），不过滤就会读到两份。
    """
    condition = iuia.CreatePropertyCondition(
        UIA.UIA_ControlTypePropertyId, UIA.UIA_TextControlTypeId)
    elements = element.FindAll(UIA.TreeScope_Descendants, condition)
    names: list[str] = []
    for index in range(elements.Length):
        name = elements.GetElement(index).CurrentName or ""
        if name:
            names.append(name)
    return names


def _all_names(iuia, element) -> list[str]:
    """按树顺序收集子树里全部非空名称（含 Group 上的名称，供诊断与系统提示用）。"""
    elements = element.FindAll(UIA.TreeScope_Descendants, iuia.CreateTrueCondition())
    names: list[str] = []
    for index in range(elements.Length):
        name = elements.GetElement(index).CurrentName or ""
        if name:
            names.append(name)
    return names


@dataclass(frozen=True)
class AccessibilityMessage:
    """聊天区里的一条消息（无障碍树直读，不经过框选复制）。"""

    # 发送人昵称；系统提示消息为空串
    sender: str
    # 消息时间戳（QQ 显示格式，如"昨天 12:42"）；该条不是时间分组首条时为空串
    timestamp: str
    # 正文（@ 原样保留，图片/表情为 QQ 给的占位文字）
    text: str
    # 子树里全部文本名称，按树顺序（诊断与兜底用）
    texts: tuple[str, ...]
    # 消息矩形 (left, top, right, bottom)，屏幕物理像素
    rect: tuple[int, int, int, int]

    def visible_in(self, area: tuple[int, int, int, int]) -> bool:
        """与消息区视口有交集即算"当前可见"（长消息允许上下超出一屏）。"""
        left, top, right, bottom = self.rect
        return (top < area[3] and bottom > area[1] and
                left < area[2] and right > area[0])


@dataclass(frozen=True)
class AccessibilityChat:
    """一次聊天区读取的结果：当前会话名 + 已渲染消息 + 消息区视口。"""

    # 当前打开的会话名（chat-header__contact-name），无需 OCR
    contact_name: str
    # 消息区视口矩形 (left, top, right, bottom)，屏幕物理像素
    area_rect: tuple[int, int, int, int]
    # 全部已渲染消息（含已滚出视口的），按界面上下顺序
    messages: tuple[AccessibilityMessage, ...]
    # 本次读取耗时（毫秒）
    elapsed_ms: float = 0.0

    def visible_messages(self) -> tuple[AccessibilityMessage, ...]:
        """只保留与消息区视口相交的消息，即界面上当前看得到的那几条。"""
        return tuple(message for message in self.messages if message.visible_in(self.area_rect))


class ContactListAccessibility:
    """会话列表无障碍树读取器（进程内单例）。

    按 hwnd 缓存窗口连接：首次 connect 约 100ms，之后枚举约 30ms。COM 元素失效
    （QQ 重启、切账号重建窗口）时自动断开重连一次，仍失败则抛
    AccessibilityUnavailableError。
    """

    # 类级单例实例
    _instance: "ContactListAccessibility | None" = None

    def __new__(cls):
        # 单例：只创建一个实例
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # 已初始化则跳过（单例 __init__ 会被重复调用）
        if hasattr(self, "_ready"):
            return
        self._ready = True
        # COM 调用串行化
        self._lock = threading.RLock()
        # 当前缓存的窗口句柄与元素
        self._hwnd = 0
        self._app = None
        self._window = None

    # ── 连接管理 ────────────────────────────────────────────────
    def _reset(self) -> None:
        """丢弃缓存的连接与元素（COM 失效后必须重建）。"""
        self._hwnd = 0
        self._app = None
        self._window = None

    def _connect(self, hwnd: int):
        """按句柄连接 QQ 进程并取窗口元素。"""
        try:
            application, _ = _uia_client()
            app = application(backend="uia").connect(handle=hwnd, timeout=CONNECT_TIMEOUT_SECONDS)
            element = app.window(handle=hwnd).element_info.element
            window = cast(element, POINTER(UIA.IUIAutomationElement))
            # 触发一次属性读取，确认元素真的可用（只 cast 不会暴露失效）
            _ = window.CurrentBoundingRectangle
        except Exception as error:
            self._reset()
            raise AccessibilityUnavailableError(
                "connect failed: {kind}: {message}".format(kind=type(error).__name__, message=error)
            ) from error
        self._hwnd = hwnd
        self._app = app
        self._window = window
        return window

    def _window_element(self, hwnd: int):
        """取（必要时重建）窗口元素。"""
        if self._hwnd != hwnd or self._window is None:
            logger.info("accessibility.connect hwnd=%s previous=%s", hwnd, self._hwnd or None)
            return self._connect(hwnd)
        return self._window

    # ── 列表元素与滚动 ──────────────────────────────────────────
    def _pane_element(self, hwnd: int):
        """取会话列表容器元素本身（不枚举行）；滚动能力挂在它上面。"""
        iuia = _iuia()
        window = self._window_element(hwnd)
        element = window.FindFirst(
            UIA.TreeScope_Descendants,
            iuia.CreatePropertyCondition(UIA.UIA_NamePropertyId, LIST_PANE_NAME),
        )
        if element is None:
            raise AccessibilityUnavailableError("pane {name!r} not found".format(name=LIST_PANE_NAME))
        return element

    def _scroll_pattern(self, hwnd: int):
        """取会话列表的 ScrollPattern 接口。"""
        element = self._pane_element(hwnd)
        return element.GetCurrentPattern(
            UIA.UIA_ScrollPatternId).QueryInterface(UIA.IUIAutomationScrollPattern)

    def scroll_state(self, hwnd: int) -> AccessibilityScroll:
        """读取列表滚动状态；容器不发布 ScrollPattern 时抛业务错误。"""
        return self._run_with_retry(hwnd, "scroll_state", self._scroll_state_once)

    def _scroll_state_once(self, hwnd: int) -> AccessibilityScroll:
        """单次读取实现（不做重试）。"""
        pattern = self._scroll_pattern(hwnd)
        return AccessibilityScroll(
            view_size_percent=float(pattern.CurrentVerticalViewSize),
            position_percent=float(pattern.CurrentVerticalScrollPercent),
            scrollable=bool(pattern.CurrentVerticallyScrollable),
        )

    def set_scroll_percent(self, hwnd: int, percent: float) -> AccessibilityScroll:
        """把列表垂直滚动位置设为百分比（0~100），返回设置后的状态。"""
        return self._run_with_retry(hwnd, "set_scroll", lambda target: self._set_scroll_once(target, percent))

    def _set_scroll_once(self, hwnd: int, percent: float) -> AccessibilityScroll:
        """单次设置实现（不做重试）。"""
        pattern = self._scroll_pattern(hwnd)
        pattern.SetScrollPercent(NO_SCROLL_PERCENT, float(percent))
        return AccessibilityScroll(
            view_size_percent=float(pattern.CurrentVerticalViewSize),
            position_percent=float(pattern.CurrentVerticalScrollPercent),
            scrollable=bool(pattern.CurrentVerticallyScrollable),
        )

    # ── 枚举 ────────────────────────────────────────────────────
    def pane(self, hwnd: int) -> AccessibilityPane:
        """枚举会话列表：返回列表区矩形与全部已渲染行。

        失败时按 ENUMERATE_ATTEMPTS 重连重试，最终抛 AccessibilityUnavailableError。
        """
        return self._run_with_retry(hwnd, "enumerate", self._pane_once)

    def chat(self, hwnd: int) -> AccessibilityChat:
        """读取当前会话：会话名 + 已渲染消息（含已滚出视口的）。

        重试语义与 pane() 一致：元素树瞬时重建时重连重试，最终抛
        AccessibilityUnavailableError 供调用方回退到截图链路。
        """
        return self._run_with_retry(hwnd, "chat", self._chat_once)

    def message_area(self, hwnd: int) -> tuple[int, int, int, int]:
        """读取消息区容器的屏幕矩形 (left, top, right, bottom)。

        与 chat() 共用同一套重连重试语义，但只取容器矩形、不枚举消息：供框选
        复制在模板锚点缺失时兜底定位消息区（真机实测"我的手机"、服务号、部分
        群聊的聊天头部没有 messagebox_top 图标，模板法会误报区域找不到）。
        """
        return self._run_with_retry(hwnd, "message_area", self._message_area_once)

    def _message_area_once(self, hwnd: int) -> tuple[int, int, int, int]:
        """单次读取消息区容器矩形（不做重试）。"""
        iuia = _iuia()
        window = self._window_element(hwnd)
        element = window.FindFirst(
            UIA.TreeScope_Descendants,
            iuia.CreatePropertyCondition(UIA.UIA_NamePropertyId, MESSAGE_AREA_NAME),
        )
        if element is None:
            raise AccessibilityUnavailableError(
                "message area {name!r} not found".format(name=MESSAGE_AREA_NAME))
        return _element_rect(element)

    def _run_with_retry(self, hwnd: int, label: str, builder):
        """元素树瞬时重建时断开重连重试；非瞬态错误直接抛出。"""
        last_error: Exception | None = None
        for attempt in range(1, ENUMERATE_ATTEMPTS + 1):
            try:
                with self._lock:
                    return builder(hwnd)
            except Exception as error:  # noqa: BLE001 - 仅重试"元素树瞬时重建"类错误
                # 非瞬态错误（真正的代码缺陷）直接抛出，不当成可回退的业务失败。
                if not is_transient_error(error):
                    raise
                # 元素树瞬时重建或 QQ 重启：断开重连再试一次；最后一次仍失败就抛业务错误。
                last_error = error
                self._reset()
                logger.warning("accessibility.%s.retry attempt=%d reason=%s", label, attempt, error)
                if attempt < ENUMERATE_ATTEMPTS:
                    time.sleep(ENUMERATE_RETRY_WAIT_SECONDS)
        raise AccessibilityUnavailableError(
            "{label} failed after {attempts} attempts: {message}".format(
                label=label, attempts=ENUMERATE_ATTEMPTS, message=last_error)
        ) from last_error

    def _pane_once(self, hwnd: int) -> AccessibilityPane:
        """单次枚举实现（不做重试）。"""
        started = time.perf_counter()
        iuia = _iuia()
        window = self._window_element(hwnd)
        descendants = UIA.TreeScope_Descendants
        # 会话列表容器：按 Name 精确查找
        pane_condition = iuia.CreatePropertyCondition(UIA.UIA_NamePropertyId, LIST_PANE_NAME)
        pane_element = window.FindFirst(descendants, pane_condition)
        if pane_element is None:
            raise AccessibilityUnavailableError("pane {name!r} not found".format(name=LIST_PANE_NAME))
        pane = _element_rect(pane_element)
        # 行元素统一是 Group，再用 ClassName 里的 recent-contact-item 过滤
        group_condition = iuia.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId, UIA.UIA_GroupControlTypeId)
        text_condition = iuia.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId, UIA.UIA_TextControlTypeId)
        elements = pane_element.FindAll(descendants, group_condition)
        rows: list[AccessibilityRow] = []
        for index in range(elements.Length):
            element = elements.GetElement(index)
            class_name = element.CurrentClassName or ""
            # 只保留会话行；右击菜单条目 ClassName 也含同名关键字，必须排除
            if ROW_CLASS_MARKER not in class_name or MENU_CLASS_MARKER in class_name:
                continue
            rect = _element_rect(element)
            texts: list[str] = []
            children = element.FindAll(descendants, text_condition)
            for child_index in range(children.Length):
                value = children.GetElement(child_index).CurrentName or ""
                if value:
                    texts.append(value)
            rows.append(
                AccessibilityRow(
                    name=texts[0] if texts else "",
                    texts=tuple(texts),
                    rect=rect,
                    selected=ROW_SELECTED_MARKER in class_name,
                    pinned=ROW_PINNED_MARKER in class_name,
                    copyable=_row_copyable(iuia, element),
                )
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        non_copyable = sum(1 for row in rows if not row.copyable)
        logger.info("accessibility.enumerate rows=%d visible=%d non_copyable=%d elapsed_ms=%.2f",
                    len(rows), sum(1 for row in rows if row.visible_in(pane)), non_copyable, elapsed_ms)
        return AccessibilityPane(rect=pane, rows=tuple(rows), elapsed_ms=elapsed_ms)

    # ── 聊天区枚举 ──────────────────────────────────────────────
    def _chat_once(self, hwnd: int) -> AccessibilityChat:
        """单次聊天区读取（不做重试）。"""
        started = time.perf_counter()
        iuia = _iuia()
        window = self._window_element(hwnd)
        descendants = UIA.TreeScope_Descendants
        # 消息区容器：QQ 给它自报 Name="消息列表"，比多段拼接的 ClassName 更稳
        area_element = window.FindFirst(
            descendants,
            iuia.CreatePropertyCondition(UIA.UIA_NamePropertyId, MESSAGE_AREA_NAME),
        )
        if area_element is None:
            raise AccessibilityUnavailableError(
                "message area {name!r} not found".format(name=MESSAGE_AREA_NAME))
        area_rect = _element_rect(area_element)
        # 当前会话名：聊天区头部自带，读它就不必再对会话列表做 OCR 校验
        name_element = window.FindFirst(
            descendants,
            iuia.CreatePropertyCondition(UIA.UIA_ClassNamePropertyId, CHAT_NAME_CLASS_MARKER),
        )
        contact_name = (name_element.CurrentName or "") if name_element is not None else ""
        # 消息：只在消息区容器范围内找根元素 ClassName == "message"
        elements = area_element.FindAll(
            descendants,
            iuia.CreatePropertyCondition(UIA.UIA_ClassNamePropertyId, MESSAGE_CLASS_NAME),
        )
        messages = tuple(
            _parse_message(iuia, elements.GetElement(index))
            for index in range(elements.Length)
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "accessibility.chat contact=%s messages=%d visible=%d elapsed_ms=%.2f",
            contact_name, len(messages),
            sum(1 for message in messages if message.visible_in(area_rect)), elapsed_ms,
        )
        return AccessibilityChat(
            contact_name=contact_name,
            area_rect=area_rect,
            messages=messages,
            elapsed_ms=elapsed_ms,
        )

    # ── 便捷查询 ────────────────────────────────────────────────
    def available(self, hwnd: int) -> bool:
        """探测无障碍树是否可用（不抛异常，供回退判断）。"""
        try:
            self.pane(hwnd)
            return True
        except AccessibilityUnavailableError:
            return False


def _parse_message(iuia, element) -> AccessibilityMessage:
    """把一条 message 元素解析成（发送人 / 时间 / 正文）。

    真机实测的子树结构（缩进表示层级）::

        message
        ├─ message__timestamp no-copy        → 子 Text = "昨天 12:42"（仅时间分组首条）
        ├─ message-container ...             （系统提示消息没有它）
        │  ├─ avatar-span                    → 子 Text = 发送人昵称
        │  └─ msg-content-container ...      → 子 Text = 正文（@ / 图片占位…）
        └─ gray-tip-message / gray-tip-action（入群提示等灰条，人名挂在 Group 名称上）
    """
    sender = ""
    timestamp = ""
    body: list[str] = []
    for child in _child_elements(iuia, element):
        class_name = child.CurrentClassName or ""
        if MESSAGE_TIMESTAMP_CLASS_MARKER in class_name:
            names = _text_names(iuia, child)
            timestamp = names[0] if names else ""
        elif MESSAGE_TIP_CLASS_MARKER in class_name:
            # 灰条提示："明麓 加入了群聊。" 人名挂在非 Text 元素名称上（可能是
            # gray-tip-message 自己，也可能是它下面一层的 gray-tip-action），正文在 Text 里
            for part in (child, *_child_elements(iuia, child)):
                part_name = part.CurrentName or ""
                if (part_name and not sender
                        and part.CurrentControlType != UIA.UIA_TextControlTypeId):
                    sender = part_name
            body.extend(_text_names(iuia, child))
        elif MESSAGE_CONTAINER_CLASS_MARKER in class_name:
            for grandchild in _child_elements(iuia, child):
                grand_class = grandchild.CurrentClassName or ""
                if MESSAGE_SENDER_CLASS_MARKER in grand_class:
                    names = _text_names(iuia, grandchild)
                    sender = names[0] if names else (grandchild.CurrentName or "")
                elif MESSAGE_CONTENT_CLASS_MARKER in grand_class:
                    # 正文容器的直接子元素：文字在 text-element 的 Text 里，
                    # 图片/表情的占位文字（"图片"/"表情"）则挂在 Group/Image 的名称上，
                    # 因此这里收全部名称而非只收 Text。
                    body.extend(_all_names(iuia, grandchild))
    return AccessibilityMessage(
        sender=sender,
        timestamp=timestamp,
        text=" ".join(body).strip(),
        texts=tuple(_all_names(iuia, element)),
        rect=_element_rect(element),
    )


def accessibility_chat(hwnd: int) -> AccessibilityChat:
    """模块级便捷入口：读取当前会话的消息（无障碍树）。"""
    return ContactListAccessibility().chat(hwnd)


def accessibility_message_area(hwnd: int) -> tuple[int, int, int, int]:
    """模块级便捷入口：读取消息区容器矩形（屏幕物理像素）。"""
    return ContactListAccessibility().message_area(hwnd)


def accessibility_scroll_state(hwnd: int) -> AccessibilityScroll:
    """模块级便捷入口：读取会话列表滚动状态。"""
    return ContactListAccessibility().scroll_state(hwnd)


def accessibility_set_scroll_percent(hwnd: int, percent: float) -> AccessibilityScroll:
    """模块级便捷入口：设置会话列表垂直滚动位置（0~100）。"""
    return ContactListAccessibility().set_scroll_percent(hwnd, percent)


def accessibility_pane(hwnd: int) -> AccessibilityPane:
    """模块级便捷入口：枚举一次会话列表。"""
    return ContactListAccessibility().pane(hwnd)


def accessibility_available(hwnd: int) -> bool:
    """模块级便捷入口：探测无障碍树是否可用。"""
    return ContactListAccessibility().available(hwnd)
