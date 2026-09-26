"""鼠标操作原语：点击、拖拽、随机取点。不依赖其他 utils 子包。

全部通过 user32 的 SetCursorPos / mouse_event 在系统底层模拟硬件级鼠标信号，
相比 pyautogui 等库更接近真实输入，降低被界面"人机检测"误判的概率。
坐标一律为物理像素（utils/__init__.py 启动时已固定 DPI-aware）。
"""

# random：在矩形内取随机点（模拟人工点击位置，避免每次都点同一点）
import random
# ctypes：调用 Win32 API（user32.dll）
import ctypes
# wintypes：Windows 原生类型（POINT 结构等）
from ctypes import wintypes
# time：按下到释放之间的间隔（模拟真实按键时长）
import time

# user32.dll 句柄：use_last_error=True 便于排查 API 失败时的错误码
_user32 = ctypes.WinDLL('user32', use_last_error=True)


def random_point(x: int, y: int, w: int, h: int) -> tuple[int, int]:
    """矩形区域内随机取一点 (不包含右/下边界，避免越界)"""
    # 横向在 [x, x+w-1] 取随机（-1 防止点到矩形右边界外）
    return (random.randint(x, x + w - 1),
            # 纵向在 [y, y+h-1] 取随机
            random.randint(y, y + h - 1))


def random_click(x: int, y: int, w: int, h: int, delay: float = 0.05, restore: bool = True):
    """矩形区域内随机取一点并左键单击。

    :param restore: True=点击后光标归位, False=留在点击位置
    """
    # 先取随机点，再调用精确点击
    px, py = random_point(x, y, w, h)
    click_at(px, py, delay, restore)


def click_at(x: int, y: int, delay: float = 0.05, restore: bool = True):
    """在屏幕坐标 (x, y) 处左键单击，系统底层 mouse_event 模拟硬件信号。

    :param delay: 按下到释放间隔
    :param restore: True=点击后光标归位, False=留在点击位置
    """
    if restore:
        # 需要归位时先记录当前光标位置（POINT 结构）
        old = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(old))
    # 移动光标到目标屏幕坐标
    _user32.SetCursorPos(x, y)
    # MOUSEEVENTF_LEFTDOWN(2)：按下左键
    _user32.mouse_event(2, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    # 按下到释放的间隔（模拟真实点击时长）
    time.sleep(delay)
    # MOUSEEVENTF_LEFTUP(4)：释放左键
    _user32.mouse_event(4, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
    if restore:
        # 归位：把光标移回点击前的位置（避免鼠标位置被"搬走"引起注意）
        _user32.SetCursorPos(old.x, old.y)


def drag(from_x, from_y, to_x, to_y, steps=10, duration=0.3):
    """模拟鼠标拖拽 —— 按下左键 → 逐帧移动 → 释放（光标不恢复）"""
    # 起点：把光标移到拖拽起始位置
    _user32.SetCursorPos(from_x, from_y)
    # 等系统响应光标移动
    time.sleep(0.08)
    # 在起点按下左键
    _user32.mouse_event(2, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    # 按下后短暂停顿
    time.sleep(0.05)
    # 分 steps 步线性插值移动到终点：模拟人手拖拽的轨迹（而非瞬间跳变）
    for i in range(1, steps + 1):
        t = i / steps  # 当前进度 0~1
        # 按进度线性插值出当前坐标
        cx = int(from_x + (to_x - from_x) * t)
        cy = int(from_y + (to_y - from_y) * t)
        _user32.SetCursorPos(cx, cy)
        # 每步间隔 = 总时长 / 步数
        time.sleep(duration / steps)
    # 到达终点后短暂停顿
    time.sleep(0.05)
    # 释放左键完成拖拽
    _user32.mouse_event(4, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
