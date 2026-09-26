"""鼠标操作原语：点击、拖拽、随机取点。不依赖其他 utils 子包。"""

import sys
from pathlib import Path
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 深一层子包

import random
import ctypes
from ctypes import wintypes
import time

_user32 = ctypes.WinDLL('user32', use_last_error=True)


def random_point(x: int, y: int, w: int, h: int) -> tuple[int, int]:
    """矩形区域内随机取一点 (不包含右/下边界，避免越界)"""
    return (random.randint(x, x + w - 1),
            random.randint(y, y + h - 1))


def random_click(x: int, y: int, w: int, h: int, delay: float = 0.05, restore: bool = True):
    """矩形区域内随机取一点并左键单击。

    :param restore: True=点击后光标归位, False=留在点击位置
    """
    px, py = random_point(x, y, w, h)
    click_at(px, py, delay, restore)


def click_at(x: int, y: int, delay: float = 0.05, restore: bool = True):
    """在屏幕坐标 (x, y) 处左键单击，系统底层 mouse_event 模拟硬件信号。

    :param delay: 按下到释放间隔
    :param restore: True=点击后光标归位, False=留在点击位置
    """
    if restore:
        old = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(old))
    _user32.SetCursorPos(x, y)
    _user32.mouse_event(2, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    time.sleep(delay)
    _user32.mouse_event(4, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
    if restore:
        _user32.SetCursorPos(old.x, old.y)


def drag(from_x, from_y, to_x, to_y, steps=10, duration=0.3):
    """模拟鼠标拖拽 —— 按下左键 → 逐帧移动 → 释放（光标不恢复）"""
    _user32.SetCursorPos(from_x, from_y)
    time.sleep(0.08)
    _user32.mouse_event(2, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    time.sleep(0.05)
    for i in range(1, steps + 1):
        t = i / steps
        cx = int(from_x + (to_x - from_x) * t)
        cy = int(from_y + (to_y - from_y) * t)
        _user32.SetCursorPos(cx, cy)
        time.sleep(duration / steps)
    time.sleep(0.05)
    _user32.mouse_event(4, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP


if __name__ == "__main__":
    try:
        from ..qq.composites import match_overflow_qq_icon
    except ImportError:
        from utils.qq.composites import match_overflow_qq_icon

    region = match_overflow_qq_icon()
    print(region)
    random_click(
        region["left"], region["top"],
        region["right"] - region["left"],
        region["bottom"] - region["top"],
    )
