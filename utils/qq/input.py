"""QQ 输入框模块：定位、聚焦、剪贴板粘贴、快捷键发送。

输入框管理器（InputBox）统管：
  - 区域定位：复用 regions.get_inputbox_region_and_image 获取输入框截图与屏幕坐标
  - 聚焦点击：在输入框内随机点击，把输入焦点交给聊天输入控件
  - 文本输入：方案 2 —— Windows 原生 win32api 写剪贴板 + Ctrl+V 粘贴，最稳定
  - 发送：按 Enter 快捷键发送（QQ 设置的"发送消息"快捷键），取代点击发送按钮

依赖 core + vision + qq/regions + qq/window_ops。
"""

# time：按键/渲染间隔
import time

# win32clipboard：系统剪贴板读写（粘贴输入法）
import win32clipboard
# win32api：keybd_event 模拟键盘按键
import win32api
# win32con：键盘常量（VK_CONTROL 等）
import win32con

# 输入框区域识别（含发送按钮坐标）
from .regions import get_inputbox_region_and_image, RegionResult
# 窗口就绪保证
from .window_ops import ensure_qq_window_with_retry
# 无焦点置顶上下文（点击前保证窗口完整在屏内）
from ..core.window import WindowCaptureCtx
# 计时装饰器
from ..core.timing import timer
# 随机点击（聚焦输入框用）
from ..core.mouse import random_click
# 快捷键发送（Enter）与快捷键配置表
from .hotkeys import send_hotkey, SEND_MESSAGE
# 用户列表（send_text 内部激活目标会话）
from .user_list import UserList


class InputBox:
    """QQ 聊天输入框管理器（单例）。

    持有输入框截图、屏幕坐标与发送按钮坐标，提供聚焦→粘贴→发送的完整流程。

    用法:
        ib = InputBox()
        ib.refresh()              # 定位输入框并聚焦输入框
        ib.paste_text("你好")     # 粘贴文本到输入框
        ib.send_message()         # 按 Enter 快捷键发送
    """

    # 类级单例实例
    _instance: "InputBox | None" = None

    def __new__(cls):
        # 单例：只创建一次
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # 已初始化过则跳过
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        # 关联的 QQ 主窗口句柄
        self.hwnd: int = 0
        # 输入框定位结果（含区域坐标与发送按钮坐标）
        self.input_region: RegionResult | None = None  # 直接存储整个定位结果（RegionResult）

    # ── 剪贴板原语（方案 2） ────────────────────────────────────
    @staticmethod
    def write_clipboard(text):
        """把文本写入系统剪贴板（显式 UNICODE + ANSI 双格式），结束时释放占用。

        乱码/编码错误的两个常见根因与对策：
          1. 传入的是 bytes —— 若按 utf-8 误当 latin1/原样塞入会乱码；
             这里统一规范化为 str（bytes 按 utf-8 解码）。
          2. 仅写 CF_UNICODETEXT，某些用 ANSI 读取的程序读到乱码 ——
             这里补写一份 CF_TEXT（GBK 编码），兼容两类读取方。
        """
        # 归一化为 str，避免 bytes 直接写入导致编码错误/乱码
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")

        # 打开剪贴板（独占访问）
        win32clipboard.OpenClipboard()
        try:
            # 清空剪贴板（避免旧内容残留）
            win32clipboard.EmptyClipboard()
            # Unicode 格式（主），QQ 等现代程序读此格式
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            # ANSI 格式（兼容老式读取方，用 GBK 编中文防乱码）
            try:
                win32clipboard.SetClipboardText(text.encode("gbk"), win32clipboard.CF_TEXT)
            except Exception:
                pass  # 个别字符无法 GBK 编码时忽略 ANSI 副本，保留 Unicode
        finally:
            # 无论成功与否都释放剪贴板占用（否则其他程序无法使用剪贴板）
            win32clipboard.CloseClipboard()

    def send_ctrl_v(self, delay: float = 0.05):
        """发送 Ctrl+V 组合键，把剪贴板内容粘贴到当前聚焦控件。"""
        # 按下 Ctrl
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)  # 按下 Ctrl
        # 按下 V
        win32api.keybd_event(ord('V'), 0, 0, 0)  # 按下 V
        # 松开 V
        win32api.keybd_event(ord('V'), 0, win32con.KEYEVENTF_KEYUP, 0)  # 松开 V
        # 松开 Ctrl
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)  # 松开 Ctrl
        # 等粘贴完成（输入框渲染）
        time.sleep(delay)

    # ── 刷新 ────────────────────────────────────────────────────
    @timer
    def refresh(self):
        """定位输入框与发送按钮并获取截图，随后点击输入框把焦点交给它。

        等价于 UserList.refresh / MessageList.refresh_messageList 的对外刷新入口：
        refresh() 一次完成"定位 + 聚焦"，之后即可 paste_text / send_message。
        窗口不就绪时抛 QQWindowNotReadyError，不静默返回 False。
        """
        # 确保 QQ 主窗口就绪（分级校验 + 重试）
        main_window = ensure_qq_window_with_retry()
        # 记录句柄（点击输入框/发送按钮时置顶窗口用）
        self.hwnd = main_window._hWnd

        # 定位输入框（截图 + 屏幕坐标），send_button_region 字段保留但发送已改快捷键
        self.input_region = get_inputbox_region_and_image(main_window)

        return True

    # ── 交互操作 ────────────────────────────────────────────────
    @timer
    def click_inputbox(self):
        """在输入框区域内随机取点点击，把输入焦点交给聊天输入控件。

        进入上下文时置顶并整理窗口形态（高度铺满 + 宽 50% + 靠左），
        确保整个输入框都在屏幕可点击范围内。
        """
        # 未 refresh 过则无法点击
        if self.input_region is None:
            print("[WARN] 尚未刷新输入框区域，先调用 refresh()")
            return
        # 取输入框屏幕区域
        region = self.input_region.screen_region
        # 置顶 + 整理窗口形态（不抢焦点），保证输入框完整在屏内
        with WindowCaptureCtx(self.hwnd, layout=True):
            # 区域内随机取点点击（模拟人工点击）
            random_click(region["x"], region["y"], region["w"], region["h"])

    def clear_text(self):
        """清空输入框当前内容（Ctrl+A 全选后粘贴空字符串覆盖）。"""
        # 按下 Ctrl
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)  # 按下 Ctrl
        # 按下 A（全选）
        win32api.keybd_event(ord('A'), 0, 0, 0)  # 按下 A
        # 松开 A
        win32api.keybd_event(ord('A'), 0, win32con.KEYEVENTF_KEYUP, 0)  # 松开 A
        # 松开 Ctrl
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)  # 松开 Ctrl
        # 等全选生效
        time.sleep(0.05)
        # 粘贴空字符串 → 覆盖选中内容即删除
        self.paste_text("")  # 空文本覆盖即删除
        time.sleep(0.05)

    def paste_text(self, text: str, delay: float = 0.05):
        """把文本写入剪贴板并粘贴到当前聚焦的输入框。

        :param text: 要输入的文本
        :param delay: 粘贴后等待时长，保证输入框完成渲染
        """
        # 先聚焦输入框（点击输入框区域）
        self.click_inputbox()  # 聚焦输入框
        # 文本写入剪贴板
        self.write_clipboard(text)
        # Ctrl+V 粘贴
        self.send_ctrl_v(delay)

    @timer
    def send_message(self):
        """通过快捷键发送：在输入框有焦点时按 Enter（QQ 设置的"发送消息"快捷键）。

        相比点击发送按钮：
          - 省一次发送按钮模板匹配（少一次视觉识别）；
          - 不受发送按钮"激活/未激活"两种图标差异影响（不用匹配按钮态）。
        前置条件：焦点已在输入框（paste_text 已 click_inputbox），且输入框有内容。
        """
        # 未 refresh 过则无法发送
        if self.input_region is None:
            print("[WARN] 尚未刷新输入框区域，先调用 refresh()")
            return
        # 焦点在输入框，按 Enter 发送（QQ 全局快捷键，当前焦点窗口即输入框）
        send_hotkey(*SEND_MESSAGE)
        # 等发送完成（消息上屏渲染，避免紧接着截图到旧画面）
        time.sleep(0.3)

    def send_text(self, contact_name: str, text: str):
        """便捷流程：内部激活目标会话 → 聚焦 → 输入文本 → 发送。

        发送前在方法内部自动激活该用户（点击列表行切换会话，已激活则跳过），
        调用方无需（也不应）先单独激活——发送本身自带激活语义。
        """
        # 内部激活用户：根据名字点击列表行切换到目标会话（已激活则跳过点击）
        UserList().active_user_by_name(contact_name)
        # 刷新定位（含聚焦）
        self.refresh()
        # 粘贴文本
        self.paste_text(text)
        # 通过快捷键发送
        self.send_message()
