"""窗口管理原语：置顶/最小化/移动/工作区左半布局、贴边检测、无焦点截屏上下文。

不依赖其他 utils 子包；QQ 专属的进程/托盘/唤起函数位于 qq/window_ops.py。

关键点：
- 所有操作"不抢焦点"：用 SWP_NOACTIVATE 系标志 + HWND_TOPMOST/NOTOPMOST 层级控制，
  避免自动化过程中把用户正在使用的其他窗口焦点抢走；
- 窗口形态统一为"工作区高度铺满 + 宽度 50% + 靠左"（layout_window_left_half），
  不再使用最大化：QQ 会话窗口无需全屏，左半工作区足以容纳列表/消息/输入框，
  且完整露出在屏内，坐标点击不会落空；
- WindowCaptureCtx 上下文管理器是"截图/点击"的标准姿势：短暂置顶露出画面 → 操作 → 自动压底。
"""

# warnings：过滤第三方库的无关告警（见下方 filterwarnings）
import warnings
# time：等待系统渲染窗口画面的延迟
import time
# win32gui：窗口枚举/位置/可见性等 GUI 查询与操作（pywin32）
import win32gui
# win32con：Win32 常量（SWP_*/HWND_*/SW_* 等）
import win32con
# ctypes：直调 user32 的 SetWindowPos/GetWindowRect/GetSystemMetrics/MonitorFromWindow/GetMonitorInfoW
import ctypes
# wintypes：ctypes 的 Win32 基础类型（RECT/DWORD 等，MONITORINFO 结构用）
from ctypes import wintypes

# pywinauto.keyboard 模块在 Python 3.12 下有 SyntaxWarning，属第三方噪音，统一静音
warnings.filterwarnings("ignore", category=SyntaxWarning, module="pywinauto.keyboard")

# 计时装饰器：给窗口层级操作自动打耗时日志
from .timing import timer


class MONITORINFO(ctypes.Structure):
    """显示器信息结构（GetMonitorInfoW 输出）。

    本实现只需 rcWork（工作区：排除任务栏的可用矩形），
    cbSize 必须预填结构大小，否则 GetMonitorInfoW 返回失败。
    """

    _fields_ = [
        ("cbSize", wintypes.DWORD),   # 结构大小（调用前填 sizeof）
        ("rcMonitor", wintypes.RECT),  # 显示器完整矩形（含任务栏区域）
        ("rcWork", wintypes.RECT),     # 工作区矩形（排除任务栏，可安全放置窗口）
        ("dwFlags", wintypes.DWORD),   # 主显示器标志（MONITORINFOF_PRIMARY 等）
    ]


class WindowCaptureCtx:
    """无焦点窗口上下文：短暂置顶露出画面，结束自动压入底层。

    仅用于截图、界面抓取，全程不抢键盘鼠标焦点。
    - 进入时 HWND_TOPMOST 强制上浮（不受前台限制）
    - 可选：layout=True 时额外把窗口整理为"工作区高度铺满 + 宽 50% + 靠左"，
      保证含发送按钮在内的控件完整在屏内、坐标点击不会落空。
      （历史参数名 maximize 已弃用——不再最大化，见 layout_window_left_half）
    - 退出时取消置顶并压底

    用法:
        with WindowCaptureCtx(hwnd):
            img = WGC.capture(hwnd)
        with WindowCaptureCtx(hwnd, layout=True):  # 点击前确保窗口完整在屏内
            random_click(...)
    """

    def __init__(self, hwnd: int, render_delay: float = 0.06, layout: bool = False):
        # 目标窗口句柄
        self.hwnd = hwnd
        # SetWindowPos 标志：不移动、不改尺寸（只改 Z 序层级）
        self.flag = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
        # 置顶后等待画面渲染的秒数（太短可能截到旧帧）
        self.delay = render_delay
        # 标记是否真正进入了有效上下文（窗口无效时 __exit__ 不做清理动作）
        self.is_valid = False
        # 是否在进入时把窗口整理为"工作区左半"形态（取代历史的最大化）
        self.layout = layout

    def __enter__(self):
        # 进入上下文：只对"存在且可见"的窗口生效
        hw = self.hwnd
        if win32gui.IsWindow(hw) and win32gui.IsWindowVisible(hw):
            # 窗口处于最小化状态时先恢复（SW_RESTORE），否则置顶后仍是缩略图
            if win32gui.IsIconic(hw):
                win32gui.ShowWindow(hw, win32con.SW_RESTORE)
            # 瞬时永久置顶（HWND_TOPMOST），100% 露出画面，不受前台锁定限制
            win32gui.SetWindowPos(hw, win32con.HWND_TOPMOST, 0, 0, 0, 0, self.flag)
            # 可选：整理窗口形态（高度铺满 + 宽 50% + 靠左），保证控件完整在可点击范围内
            if self.layout:
                layout_window_left_half(hw)
            # 等系统渲染画面完成，再让调用方截图/点击
            time.sleep(self.delay)  # 等系统渲染画面完成
            # 标记有效：__exit__ 时需要撤销置顶
            self.is_valid = True
        # 返回自身（with ... as ctx 拿到的是本对象）
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 退出上下文：撤销置顶，恢复普通层级
        hw = self.hwnd
        if self.is_valid and win32gui.IsWindow(hw):
            # HWND_NOTOPMOST：取消置顶，让窗口回到普通 Z 序（不再挡在其他窗口前）
            win32gui.SetWindowPos(hw, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, self.flag)
        # 返回 False：不吞掉上下文内抛出的异常
        return False


@timer
def set_window_z_pos(hwnd: int, bring_front: bool = True) -> bool:
    """单布尔参数控制窗口前后层级（仅 Z 序，不抢焦点）。

    :param hwnd: 窗口句柄
    :param bring_front: True=置顶, False=压至底层
    :return: 是否执行成功
    """
    # 非法句柄直接失败
    if hwnd <= 0:
        return False
    try:
        # 最小化的窗口先恢复，否则层级操作对缩略图无效
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # 标志：不改位置、不改尺寸，只动 Z 序
        flag = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
        if bring_front:
            # 提到最前（HWND_TOP，非永久置顶）
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0, flag)
        else:
            # 压到最底（HWND_BOTTOM）
            win32gui.SetWindowPos(hwnd, win32con.HWND_BOTTOM, 0, 0, 0, 0, flag)
        return True
    except Exception as e:
        # 操作失败打印原因并返回 False（调用方决定是否重试/兜底）
        print("窗口层级操作失败：", e)
        return False


def minimize_window(hwnd: int):
    """最小化指定窗口"""
    win32gui.ShowWindow(hwnd, 6)  # SW_MINIMIZE


def maximize_window(hwnd: int):
    """最大化指定窗口（历史原语：已被 layout_window_left_half 取代，仅作兼容保留）"""
    win32gui.ShowWindow(hwnd, 3)  # SW_MAXIMIZE


@timer
def layout_window_left_half(hwnd: int, width_ratio: float = 0.5) -> None:
    """把窗口整理为"工作区高度铺满 + 宽度占工作区 width_ratio + 靠左"的形态。

    取代最大化：QQ 会话窗口无需全屏，左半工作区足以容纳列表/消息/输入框，
    且窗口完整露出在屏内（不会因部分出屏导致后续坐标点击落到桌面）。

    实现步骤：
      1. 最小化/最大化状态先还原为普通态（最大化窗口 SetWindowPos 改尺寸会被系统钳制）；
      2. 取窗口所在显示器的工作区 rcWork（已排除任务栏）；
      3. SetWindowPos 定位：x=工作区左缘, y=工作区上缘,
         w=工作区宽×width_ratio, h=工作区高；
      4. 标志：不抢焦点（SWP_NOACTIVATE）、不动 Z 序（SWP_NOZORDER）。

    :param hwnd: 目标窗口句柄
    :param width_ratio: 窗口宽度占工作区宽度的比例（默认 0.5 = 左半屏）
    """
    # 最小化（IsIconic）或最大化（WS_MAXIMIZE 样式位）→ 先还原普通态。
    # 注意：win32gui 无 IsZoomed，最大化用 GetWindowLong(GWL_STYLE) 的 WS_MAXIMIZE 位判断。
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    if win32gui.IsIconic(hwnd) or (style & win32con.WS_MAXIMIZE):
        # SW_RESTORE：还原为普通窗口（SWP_NOACTIVATE 由下方 SetWindowPos 保证不抢焦点）
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    # 取窗口所在显示器（MONITOR_DEFAULTTONEAREST=2：距离窗口最近的显示器）
    monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)  # 必须预填结构大小，否则 API 失败
    ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    work = info.rcWork  # 工作区矩形（左/上/右/下）

    # 目标尺寸：宽度 = 工作区宽 × 比例，高度 = 工作区高（铺满）
    width = int((work.right - work.left) * width_ratio)
    height = work.bottom - work.top
    # 定位到工作区左缘、上缘；不激活（0x0010 SWP_NOACTIVATE）、不动 Z 序（0x0004 SWP_NOZORDER）
    ctypes.windll.user32.SetWindowPos(
        hwnd, 0, work.left, work.top, width, height,
        win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER,
    )


def move_window(hwnd: int, x: int, y: int):
    """移动窗口到指定屏幕坐标"""
    # SetWindowPos：0x0001(SWP_NOSIZE 不改变尺寸) | 0x0004(SWP_NOZORDER 不动层级)
    ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0, 0x0001 | 0x0004)  # SWP_NOSIZE | SWP_NOZORDER


def detach_from_edge(hwnd: int):
    """检测窗口贴边后用 SetWindowPos 强制拉离边缘。

    场景：窗口部分移出屏幕时，屏幕坐标点击会点到桌面；先把它拉回屏内再操作。
    """
    # 取窗口当前矩形
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    # 屏幕宽高（GetSystemMetrics：0=宽, 1=高）
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)

    # 目标位置初始化：先假设不动
    x, y = rect.left, rect.top
    # 靠左贴边 → 右移到 x=100
    if rect.left <= 0:
        x = 100  # 靠左 → 拉到 100
    # 靠右贴边 → 左移，右侧留 100px 边距
    elif rect.right >= sw:
        x = sw - (rect.right - rect.left) - 100  # 靠右 → 留 100px 边距
    # 靠上贴边 → 下移到 y=100
    if rect.top <= 0:
        y = 100
    # 靠下贴边 → 上移，底部留 100px 边距
    elif rect.bottom >= sh:
        y = sh - (rect.bottom - rect.top) - 100

    # 只有确实需要移动时才执行
    if x != rect.left or y != rect.top:
        # 先强制恢复（防最小化状态），再移位置
        win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
        # 等恢复动画完成
        time.sleep(0.1)
        # 移动到目标位置：不改变尺寸、不动层级
        ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0,
                                          0x0001 | 0x0004)  # NOSIZE | NOZORDER
        # 等窗口落位（移动后系统需要一点时间重绘）
        time.sleep(0.3)  # 等窗口落位
