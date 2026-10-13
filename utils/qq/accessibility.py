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

聊天区同样被完整暴露，因此"读消息"也不必再框选复制+OCR（真机实测 1280x1390）：

- chat-header__contact-name 直接给出当前会话名，不必对会话列表做 OCR 复核；
- chat-msg-area__vlist（Name="消息列表"）下每条 message 元素含
  message__timestamp / avatar-span / msg-content-container，
  即时间、发送人、正文（@ 原样保留，图片与表情为"图片"/"表情"占位）；
- 一次读取约 90ms（热读 74ms），对比现网框选复制同内容 1770ms；
- 树里同样保留已滚出视口的消息（rect 为负），按消息区矩形求交集即得"当前可见"。

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
# Application：按窗口句柄连接进程
from pywinauto.application import Application
# IUIA：pywinauto 持有的 IUIAutomation 单例（避免自建 COM 对象）
from pywinauto.uia_defines import IUIA


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
            app = Application(backend="uia").connect(handle=hwnd, timeout=CONNECT_TIMEOUT_SECONDS)
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
        iuia = IUIA().iuia
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
                )
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info("accessibility.enumerate rows=%d visible=%d elapsed_ms=%.2f",
                    len(rows), sum(1 for row in rows if row.visible_in(pane)), elapsed_ms)
        return AccessibilityPane(rect=pane, rows=tuple(rows), elapsed_ms=elapsed_ms)

    # ── 聊天区枚举 ──────────────────────────────────────────────
    def _chat_once(self, hwnd: int) -> AccessibilityChat:
        """单次聊天区读取（不做重试）。"""
        started = time.perf_counter()
        iuia = IUIA().iuia
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


def accessibility_pane(hwnd: int) -> AccessibilityPane:
    """模块级便捷入口：枚举一次会话列表。"""
    return ContactListAccessibility().pane(hwnd)


def accessibility_available(hwnd: int) -> bool:
    """模块级便捷入口：探测无障碍树是否可用。"""
    return ContactListAccessibility().available(hwnd)
