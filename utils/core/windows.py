"""窗口管理原语：枚举、DPI、托盘操作、移动/缩放。不依赖其他 utils 子包。

QQ 专用组合函数（activate_qq、get_qq_window_image 等）已迁至 qq/composites.py。
"""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 深一层子包

import warnings
import time
import psutil
from pywinauto import Desktop
import pygetwindow as gw
import win32process
import win32gui
import win32con
import ctypes
import os

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pywinauto.keyboard")


def start_qq():
    """唤醒登录"""
    os.startfile("tencent://")


def get_qq_pids():
    """获取所有 QQ.exe 进程的 PID 列表"""
    qq_pids = []
    for proc in psutil.process_iter(["name"]):  # 遍历所有进程
        if proc.info["name"].lower() == "qq.exe":  # 匹配进程名
            qq_pids.append(proc.pid)  # 收集 PID
    return qq_pids


def get_qq_windows():
    """遍历所有可见窗口，通过 PID 匹配哪些属于 QQ"""
    qq_pids = get_qq_pids()  # 获取 QQ 所有 PID
    if not qq_pids:
        print("[FAIL] QQ.exe 未运行")
        return []

    qq_windows = []
    for win in gw.getAllWindows():
        title = win.title.strip()  # 窗口标题
        # if not title or not win.visible:  # 跳过无标题或不可见
        #     continue
        _, pid = win32process.GetWindowThreadProcessId(win._hWnd)  # 获取窗口 PID
        if pid in qq_pids:  # PID 匹配则属于 QQ
            qq_windows.append(win)
            print(pid, win.title)

    print(f"[OK] QQ 可见窗口共 {len(qq_windows)} 个")
    return qq_windows


def close_all_qq_windows():
    """关闭所有 QQ 可见窗口"""
    windows = get_qq_windows()  # 获取所有 QQ 窗口
    if not windows:
        print("[INFO] 没有 QQ 窗口需要关闭")
        return

    for win in windows:
        title = win.title.strip()  # 窗口标题
        try:
            win32gui.PostMessage(win._hWnd, 0x0010, 0, 0)  # WM_CLOSE 关闭窗口
            print(f"[OK] 已关闭: 「{title}」")
        except:
            print(f"[WARN] 关闭失败: 「{title}」")


def clickExpandBtn():
    """点击任务栏托盘溢出区的展开按钮"""
    # 桌面
    desktop_uia = Desktop(backend="uia")
    # 任务栏的顶层窗口
    shell_tray = desktop_uia.window(class_name="Shell_TrayWnd")
    tray_notify = shell_tray.child_window(class_name="TrayNotifyWnd")  # 右下角托盘容器

    EXPAND_BTN_TEXTS = {"通知 V 形", "显示隐藏的图标", "Show hidden icons"}
    for ctrl in tray_notify.descendants(control_type="Button"):
        txt = ctrl.window_text().strip()  # 按钮文本
        cls = ctrl.class_name()
        if cls == "Button" and txt in EXPAND_BTN_TEXTS:  # 匹配展开按钮
            ctrl.click_input()  # 点击
            print(f"[OK] 已点击托盘展开按钮")
            time.sleep(0.3)
            return True
    print("[WARN] 未找到托盘展开按钮")
    return False


def click_qq_tray_icon(qq_number=""):
    """在托盘溢出窗口中查找 QQ ,还要根据QQ号筛选,如果有多个QQ同时运行"""
    desktop_uia = Desktop(backend="uia")  # UIA 桌面
    overflow = desktop_uia.window(class_name="NotifyIconOverflowWindow")  # 溢出窗口
    if not overflow.exists():
        clickExpandBtn()
    toolbar = overflow.child_window(class_name="ToolbarWindow32")  # 图标工具栏
    for btn in toolbar.children():
        btn_text = btn.window_text().strip()  # 图标提示文本

        if "QQ" in btn_text and qq_number in btn_text and "音乐" not in btn_text:  # 匹配 QQ，排除 QQ音乐
            btn.click_input()  # 点击唤醒
            print(f"[OK] 已点击 QQ 托盘图标: 「{btn_text}」")
            time.sleep(0.5)
            return True
    print('-' * 20)
    print("[FAIL] 未在托盘溢出区找到 QQ 图标")
    return False


def timer(func):
    def wrapper(*args, **kwargs):
        t1 = time.time()
        res = func(*args, **kwargs)
        t2 = time.time()
        print(f"【{func.__name__}】耗时：{(t2 - t1) * 1000:.2f} ms")
        return res

    return wrapper


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


def get_main_qq_windows():
    """通过关闭QQ窗口,再通过后台的图标打开,从而获得QQ唯一窗口"""
    close_all_qq_windows()
    click_qq_tray_icon()
    return get_qq_windows()[0]


def get_overflow():
    """截取任务栏托盘溢出窗口的屏幕截图 → PIL.Image

    用 ImageGrab 而非 WGC —— 系统托盘窗口受保护，WGC 无法捕获。
    """
    from PIL import ImageGrab  # 屏幕区域截图 (系统窗口不受 WGC 限制)

    time.sleep(0.3)  # 等弹出动画完成

    desktop_uia = Desktop(backend="uia")  # UIA 桌面根
    overflow = desktop_uia.window(
        class_name="NotifyIconOverflowWindow")  # 溢出窗口类名
    if not overflow.exists():  # 没展开成功
        return None

    rect = overflow.rectangle()  # pywinauto 矩形对象
    bbox = (rect.left, rect.top,  # 屏幕像素坐标
            rect.right, rect.bottom)
    return ImageGrab.grab(bbox=bbox), bbox  # PIL.Image (RGB) # 屏幕像素坐标


# ── getDPI / calc_buf_size 已迁至 core/screenshot.py ────────────
# ── get_qq_window_image / activate_qq 已迁至 qq/composites.py ──


if __name__ == "__main__":
    # clickExpandBtn()
    # click_qq_tray_icon()
    get_qq_windows()
    # print(get_qq_pids())
    # import time
    # wind = get_main_qq_windows()
    # start_qq()
    # get_qq_window_image().show()   # 已迁至 qq/composites
