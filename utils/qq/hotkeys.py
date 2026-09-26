"""QQ 全局快捷键配置与发送原语（仅本项目实际用到的几个）。

为什么单独成表：QQ 的全局快捷键由用户在设置里自定义，
本项目只依赖其中三个；以后用户改键，只需改本文件的映射表，
无需改动任何业务代码。

当前键位（对应 QQ 设置中的"快捷键"页，见用户截图）：
- 打开或隐藏 QQ 所有窗口 : Ctrl + Alt + X   （有进程无窗口时唤醒用）
- 打开最新未读消息窗口   : Ctrl + Alt + Z
- 发送消息               : Enter              （输入框内发送）
"""

# ctypes：直调 user32 的 keybd_event 模拟键盘
import ctypes

# win32con：键盘常量（KEYEVENTF_KEYUP 等）
import win32con

# user32.dll 句柄：use_last_error=True 便于排查 API 失败时的错误码
_user32 = ctypes.WinDLL('user32', use_last_error=True)

# 修饰键虚拟键码（VK_*）
_MODIFIERS = {
    "ctrl": 0x11,   # VK_CONTROL
    "alt": 0x12,    # VK_MENU
    "shift": 0x10,  # VK_SHIFT
    "win": 0x5B,    # VK_LWIN
}

# 具名单键虚拟键码（VK_*）
_KEYS = {
    "enter": 0x0D,  # VK_RETURN
    "tab": 0x09,    # VK_TAB
    "esc": 0x1B,    # VK_ESCAPE
    "space": 0x20,  # VK_SPACE
}

# ── 快捷键配置表（用户改键只需改这里） ─────────────────────────
# 打开或隐藏 QQ 所有窗口：有进程但没有窗口时按下可唤醒主面板
TOGGLE_QQ_WINDOWS = ("ctrl", "alt", "x")
# 打开最新未读消息窗口
OPEN_LATEST_UNREAD_WINDOW = ("ctrl", "alt", "z")
# 发送消息（输入框有焦点时按下）
SEND_MESSAGE = ("enter",)


def _vk(key: str) -> int:
    """把键名转为虚拟键码：修饰键/具名单键查表，单字符取大写 ASCII。

    单字符（如 x/z/a）的 ASCII 码恰好等于其虚拟键码，直接 ord 即可。
    """
    # 修饰键查表
    if key in _MODIFIERS:
        return _MODIFIERS[key]
    # 具名单键查表
    if key in _KEYS:
        return _KEYS[key]
    # 单字符：大写 ASCII 即虚拟键码
    return ord(key.upper())


def send_hotkey(keys: tuple[str, ...]) -> None:
    """模拟按下并释放一组组合键（QQ 全局快捷键由系统注册，无需窗口在前台）。

    按键顺序：按下全部修饰键（保持表序）→ 按下/松开主键 → 逆序松开修饰键。
    :param keys: 键名元组，如 ("ctrl", "alt", "x") 或 ("enter",)
    """
    # 修饰键 = 键名命中 _MODIFIERS 的全部项
    mods = [k for k in keys if k in _MODIFIERS]
    # 主键 = 最后一个非修饰键（组合键只应有一个主键）
    main_keys = [k for k in keys if k not in _MODIFIERS]
    main = main_keys[-1] if main_keys else None

    # 依次按下所有修饰键
    for k in mods:
        _user32.keybd_event(_vk(k), 0, 0, 0)
    # 主键：按下再松开（若存在）
    if main is not None:
        vk = _vk(main)
        _user32.keybd_event(vk, 0, 0, 0)
        _user32.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    # 逆序松开所有修饰键
    for k in reversed(mods):
        _user32.keybd_event(_vk(k), 0, win32con.KEYEVENTF_KEYUP, 0)
