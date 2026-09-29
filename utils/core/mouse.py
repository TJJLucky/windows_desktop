"""QQ 鼠标操作原语。

本文件是 Python 与 C++ DLL 之间的“翻译层”，业务代码不直接调用 DLL：

``utils.qq`` -> ``random_click / click_at / drag`` -> 本文件 -> ``input_event.dll`` -> Windows ``SendInput``

``input_event.dll`` 从 QQPilot 的 ``InputEvent`` C++ 源码编译而来。Python
通过 :mod:`ctypes` 在运行时加载 DLL，再按 C++ 导出函数的参数/返回值约定调用它。
这里的所有坐标均为物理像素；DPI-aware 在 ``utils/__init__.py`` 中统一设置。
"""

# ctypes 是 Python 标准库，可直接加载 DLL、声明 C++ 函数签名、执行 DLL 导出函数。
import ctypes
# 点击区域内随机选点仍由 Python 完成；真正的移动和点击由 DLL 完成。
import random
# Python 保留“按下鼠标后等待多久”的时间控制。
import time
# wintypes 提供与 Win32 C 类型同宽的 Python 类型，例如 UINT、BOOL、POINT。
from ctypes import wintypes
# Path 用于稳定定位项目根目录下的 DLL，不依赖当前命令行所在目录。
from pathlib import Path


# user32.dll 是 Windows 系统库。这里只用它读取“当前鼠标在哪里”，以便点击后归位；
# 鼠标移动、按下、抬起本身由下方的 InputEvent.dll 执行。
_user32 = ctypes.WinDLL("user32", use_last_error=True)

# __file__ 是当前 mouse.py 的绝对路径：
#   <项目根>/utils/core/mouse.py
# parents[2] 回到 <项目根>，从而无论服务从哪里启动，都能找到 <项目根>/libs/input_event.dll。
_dll_path = Path(__file__).resolve().parents[2] / "libs" / "input_event.dll"
if not _dll_path.is_file():
    # 不做静默回退。缺 DLL 时若改用另一套输入实现，发布环境与开发环境会表现不同，
    # 更容易出现“本机能发消息、部署后不能”的问题。
    raise FileNotFoundError(f"QQ native input DLL not found: {_dll_path}")

# WinDLL 将文件加载进当前 Python 进程。DLL 只加载一次，后续 _input_event.Xxx(...) 就是
# 调用 DLL 内 ``extern "C" __declspec(dllexport)`` 导出的 Xxx 函数。
_input_event = ctypes.WinDLL(str(_dll_path), use_last_error=True)

# ctypes 不知道 C++ 函数参数的类型，必须在调用前声明：
#
#   C++: bool Mousegoto(unsigned x, unsigned y)
#   Python: argtypes = (UINT, UINT), restype = BOOL
#
# 没有这些声明时，ctypes 默认把参数当成 int，64 位进程中可能造成栈/返回值解释错误。
# 下列函数名必须与 native/input_event/dllmain.cpp 的导出函数名完全一致。
_input_event.Mousegoto.argtypes = (wintypes.UINT, wintypes.UINT)
_input_event.Mousegoto.restype = wintypes.BOOL
_input_event.SmoothMousegoto.argtypes = (wintypes.UINT, wintypes.UINT)
_input_event.SmoothMousegoto.restype = wintypes.BOOL
_input_event.LmouseDown.argtypes = ()
_input_event.LmouseDown.restype = wintypes.BOOL
_input_event.LmouseUp.argtypes = ()
_input_event.LmouseUp.restype = wintypes.BOOL
# 右键与左键一样由 DLL 注入；单独声明可避免 ctypes 以错误的默认签名调用无参数函数。
_input_event.RmouseDown.argtypes = ()
_input_event.RmouseDown.restype = wintypes.BOOL
_input_event.RmouseUp.argtypes = ()
_input_event.RmouseUp.restype = wintypes.BOOL
_input_event.dragFromTo.argtypes = (wintypes.UINT, wintypes.UINT, wintypes.UINT, wintypes.UINT, ctypes.c_float)
_input_event.dragFromTo.restype = wintypes.BOOL


def _call(function_name: str, *args) -> None:
    """按导出函数名调用 DLL；C++ 返回 ``false`` 时转换为 Python 异常。

    例如 ``_call("LmouseDown")`` 等同于执行 C++ 中导出的 ``LmouseDown()``。
    ``getattr`` 的作用是按字符串从已加载 DLL 上取出同名函数对象。
    """
    if not getattr(_input_event, function_name)(*args):
        # use_last_error=True 让 ctypes 保存当前线程的 Win32 LastError；抛出的异常会带上
        # Windows 错误码，Service 层可以记录该失败而不是继续误操作 QQ。
        raise ctypes.WinError(ctypes.get_last_error())


def _require_screen_coordinate(x: int, y: int) -> None:
    """校验 DLL 能接受的坐标范围。

    上游 C++ 签名使用 ``unsigned``，且主动拒绝大于 32768 的数；因此这里提前校验，
    防止 Python 的负坐标被转换为巨大的无符号数后点到错误位置。
    """
    if not 0 <= x <= 32768 or not 0 <= y <= 32768:
        raise ValueError(f"鼠标坐标超出 InputEvent 支持范围: ({x}, {y})")


def _cursor_position() -> tuple[int, int]:
    """读取真实鼠标光标坐标，仅用于 ``restore=True`` 的归位行为。"""
    point = wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return point.x, point.y


def smooth_move_to(x: int, y: int) -> None:
    """沿 QQPilot 的 WindMouse 轨迹移动，并以精确末点收束。

    ``SmoothMousegoto`` 在 C++ 内部生成多个中间点，逐个通过 ``SendInput`` 移动。
    随后的 ``Mousegoto`` 仍由同一 DLL 调用，只负责将最终像素精确设为 ``(x, y)``。
    """
    _require_screen_coordinate(x, y)
    _call("SmoothMousegoto", x, y)
    # 上游 WindMouse 的最后一个轨迹点可能距目标不到一个像素；收束保证点击不偏移，
    # 特别是输入框、列表行等窄区域的点击不能依赖“足够接近”。
    _call("Mousegoto", x, y)


def random_point(x: int, y: int, w: int, h: int) -> tuple[int, int]:
    """在矩形区域内随机取一点（不包含右/下边界）。"""
    if w <= 0 or h <= 0:
        raise ValueError("点击区域的宽高必须大于 0")
    return random.randint(x, x + w - 1), random.randint(y, y + h - 1)


def random_click(x: int, y: int, w: int, h: int, delay: float = 0.05, restore: bool = True) -> None:
    """在矩形区域内随机取一点并左键单击。

    随机选点是 Python 侧逻辑；选定后仍由 ``click_at`` 进入 C++ DLL 完成鼠标操作。
    """
    click_at(*random_point(x, y, w, h), delay=delay, restore=restore)


def click_at(x: int, y: int, delay: float = 0.05, restore: bool = True) -> None:
    """平滑移动到目标后点击；按原有约定可选地恢复鼠标位置。

    对应的 C++ 调用顺序为：

    ``SmoothMousegoto -> Mousegoto -> LmouseDown -> 等待 delay -> LmouseUp``。
    """
    # 先记住用户原来的鼠标位置；False 时不读取也不恢复，适用于后续动作需要保留焦点的场景。
    old = _cursor_position() if restore else None
    smooth_move_to(x, y)
    _call("LmouseDown")
    time.sleep(delay)
    _call("LmouseUp")
    if old is not None:
        # 归位同样走 DLL 的平滑移动，而不是 Python SetCursorPos，确保输入实现只有一套。
        smooth_move_to(*old)


def random_right_click(x: int, y: int, w: int, h: int, delay: float = 0.05, restore: bool = True) -> None:
    """在矩形区域内随机取一点并右键单击。

    适用于 QQ 的上下文菜单操作，例如对消息、联系人或输入区域执行菜单动作。
    """
    right_click_at(*random_point(x, y, w, h), delay=delay, restore=restore)


def right_click_at(x: int, y: int, delay: float = 0.05, restore: bool = True) -> None:
    """平滑移动到目标后执行一次右键点击；可选地恢复原鼠标位置。

    C++ 调用顺序为：

    ``SmoothMousegoto -> Mousegoto -> RmouseDown -> 等待 delay -> RmouseUp``。
    """
    old = _cursor_position() if restore else None
    smooth_move_to(x, y)
    _call("RmouseDown")
    time.sleep(delay)
    _call("RmouseUp")
    if old is not None:
        smooth_move_to(*old)


def drag(from_x: int, from_y: int, to_x: int, to_y: int, steps: int = 10, duration: float = 0.3) -> None:
    """平滑移动到拖拽起点，再由 DLL 按线性时间轨迹执行拖拽。

    ``steps`` 为兼容旧调用保留；上游 ``dragFromTo`` 固定以约 10 ms 一步执行。
    ``duration`` 会以 C++ ``float`` 传给 DLL，单位为秒。
    """
    # 旧 Python 实现由 steps 控制插值次数。现在由 DLL 负责插值，故保留参数但不再使用，
    # 这样现有业务调用不需要改动。
    del steps
    if duration < 0:
        raise ValueError("duration 不能小于 0")
    for x, y in ((from_x, from_y), (to_x, to_y)):
        _require_screen_coordinate(x, y)
    # 先使用 WindMouse 走到拖拽起点，再调用 DLL 的完整按下/移动/抬起流程。
    smooth_move_to(from_x, from_y)
    _call("dragFromTo", from_x, from_y, to_x, to_y, ctypes.c_float(duration))
