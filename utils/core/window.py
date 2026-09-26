"""窗口管理原语：置顶/最小化/最大化/移动、贴边检测、无焦点截屏上下文。

不依赖其他 utils 子包；QQ 专属的进程/托盘/唤起函数位于 qq/window_ops.py。
"""

import warnings
import time
import win32gui
import win32con
import ctypes

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pywinauto.keyboard")

from .timing import timer


class WindowCaptureCtx:
    """无焦点窗口上下文：短暂置顶露出画面，结束自动压入底层。

    仅用于截图、界面抓取，全程不抢键盘鼠标焦点。
    - 进入时 HWND_TOPMOST 强制上浮（不受前台限制）
    - 可选：maximize=True 时额外最大化撑满工作区，
      避免窗口部分移出屏幕而导致后续 screen 坐标点击不到。
    - 退出时取消置顶并压底

    用法:
        with WindowCaptureCtx(hwnd):
            img = WGC.capture(hwnd)
        with WindowCaptureCtx(hwnd, maximize=True):  # 点击前确保窗口完整在屏内
            random_click(...)
    """

    def __init__(self, hwnd: int, render_delay: float = 0.06, maximize: bool = False):
        self.hwnd = hwnd
        self.flag = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
        self.delay = render_delay
        self.is_valid = False
        self.maximize = maximize

    def __enter__(self):
        hw = self.hwnd
        if win32gui.IsWindow(hw) and win32gui.IsWindowVisible(hw):
            if win32gui.IsIconic(hw):
                win32gui.ShowWindow(hw, win32con.SW_RESTORE)
            # 瞬时永久置顶，100% 露出画面，不受前台锁定限制
            win32gui.SetWindowPos(hw, win32con.HWND_TOPMOST, 0, 0, 0, 0, self.flag)
            # 可选：最大化撑满工作区，保证含发送按钮在内的控件都在可点击范围内
            if self.maximize:
                win32gui.ShowWindow(hw, win32con.SW_MAXIMIZE)
            time.sleep(self.delay)  # 等系统渲染画面完成
            self.is_valid = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        hw = self.hwnd
        if self.is_valid and win32gui.IsWindow(hw):
            # 取消置顶，恢复普通层级
            win32gui.SetWindowPos(hw, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, self.flag)
        return False


@timer
def set_window_z_pos(hwnd: int, bring_front: bool = True) -> bool:
    """单布尔参数控制窗口前后层级（仅 Z 序，不抢焦点）。

    :param hwnd: 窗口句柄
    :param bring_front: True=置顶, False=压至底层
    :return: 是否执行成功
    """
    if hwnd <= 0:
        return False
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        flag = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
        if bring_front:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0, flag)
        else:
            win32gui.SetWindowPos(hwnd, win32con.HWND_BOTTOM, 0, 0, 0, 0, flag)
        return True
    except Exception as e:
        print("窗口层级操作失败：", e)
        return False


def minimize_window(hwnd: int):
    """最小化指定窗口"""
    win32gui.ShowWindow(hwnd, 6)  # SW_MINIMIZE


def maximize_window(hwnd: int):
    """最大化指定窗口"""
    win32gui.ShowWindow(hwnd, 3)  # SW_MAXIMIZE


def move_window(hwnd: int, x: int, y: int):
    """移动窗口到指定屏幕坐标"""
    ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0, 0x0001 | 0x0004)  # SWP_NOSIZE | SWP_NOZORDER


def detach_from_edge(hwnd: int):
    """检测窗口贴边后用 SetWindowPos 强制拉离边缘"""
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)

    x, y = rect.left, rect.top
    if rect.left <= 0:
        x = 100  # 靠左 → 拉到 100
    elif rect.right >= sw:
        x = sw - (rect.right - rect.left) - 100  # 靠右 → 留 100px 边距
    if rect.top <= 0:
        y = 100
    elif rect.bottom >= sh:
        y = sh - (rect.bottom - rect.top) - 100

    if x != rect.left or y != rect.top:
        # 先强制恢复（防最小化），再移位置
        win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.1)
        ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0,
                                          0x0001 | 0x0004)  # NOSIZE | NOZORDER
        time.sleep(0.3)  # 等窗口落位
