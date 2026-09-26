"""WGC 后台窗口截图与 DPI 工具。不依赖其他 utils 子包。

窗口像素只允许来自 ``wgc`` 对明确 HWND 的原生后台捕获。
"""

import ctypes
from ctypes import wintypes
import numpy as np
from PIL import Image
import os
import win32gui
import win32con
import win32print


# ── DPI ────────────────────────────────────────────────────────────
def ensure_dpi_aware():
    """启动时固定进程 DPI-aware（per-monitor），使 GetWindowRect/WGC/SetCursorPos 统一为物理像素。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        return True
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        return True
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:
        return False


def getDPI():
    """获取系统 DPI 缩放比例。96=100% 基准，如 144→1.5 倍。"""
    hdc = win32gui.GetDC(0)  # 桌面 DC
    dpi_x = win32print.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
    dpi_y = win32print.GetDeviceCaps(hdc, 90)  # LOGPIXELSY
    win32gui.ReleaseDC(0, hdc)
    DPI = dpi_x / 96.0
    print("[DPI]:", DPI)
    return DPI  # 缩放比例



def calc_buf_size(hwnd):
    """根据窗口逻辑尺寸 × 系统 DPI 缩放，计算 WGC 截图所需缓冲区字节数。

    GetWindowRect 在非 DPI-aware 进程返回 DPI 虚拟化坐标，
    WGC 返回物理像素。比值 = 系统 DPI 缩放系数。
    buf_size = logic_w × scale × logic_h × scale × 4 + 10% 安全余量。

    返回 (buf_size, logic_w, logic_h, scale)
    """
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    logic_w = rect.right - rect.left
    logic_h = rect.bottom - rect.top

    if logic_w <= 0 or logic_h <= 0:
        return 0, 0, 0, 0.0

    scale = getDPI()
    buf_size = int(logic_w * scale * logic_h * scale * 4 * 1.1)  # 额外给1.1倍的空间
    return buf_size, logic_w, logic_h, scale


# ── WGC 捕获 ───────────────────────────────────────────────────────
class WGCCapture:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._buf_cache = None
            cls._instance._max_buf_len = 0
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        dll_path = os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", "..", "libs", "wgc_capture.dll"))  # 深一层
        if not os.path.exists(dll_path):
            raise FileNotFoundError(f"DLL not found: {dll_path}")
        self._dll_handle = ctypes.CDLL(dll_path)
        self.WGCDLL = self._dll_handle

        # DLL 导出: int WgcSnapshot(HWND, uint8_t*, int, int*, int*)
        self.WGCDLL.WgcSnapshot.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self.WGCDLL.WgcSnapshot.restype = ctypes.c_int
        self._initialized = True

    @classmethod
    def release(cls):
        """仅重置内存缓存与实例标记，不再手动卸载DLL"""
        if cls._instance is not None:
            cls._instance._buf_cache = None
            cls._instance._max_buf_len = 0
            cls._instance._initialized = False
            cls._instance = None

    def _get_reuse_buffer(self, need_size: int):
        """缓冲区复用，减少频繁内存分配"""
        if self._buf_cache is None or need_size > self._max_buf_len:
            self._buf_cache = (ctypes.c_uint8 * need_size)()
            self._max_buf_len = need_size
        return self._buf_cache

    def capture(self, hwnd: int) -> Image.Image | None:
        # 窗口有效性校验
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return None
        # 禁止最小化窗口捕获
        placement = win32gui.GetWindowPlacement(hwnd)
        if placement[1] == win32con.SW_SHOWMINIMIZED:
            return None

        buf_size, *_ = calc_buf_size(hwnd)
        if buf_size <= 0:
            return None

        buf = self._get_reuse_buffer(buf_size)
        out_w = ctypes.c_int(0)
        out_h = ctypes.c_int(0)

        ret = self.WGCDLL.WgcSnapshot(
            hwnd,
            buf,
            buf_size,
            ctypes.byref(out_w),
            ctypes.byref(out_h)
        )

        if ret != 1:
            return None

        w = out_w.value
        h = out_h.value
        pixel_len = w * h * 4
        arr = np.frombuffer(buf, dtype=np.uint8)[:pixel_len]
        arr = arr.reshape(h, w, 4)

        img = Image.fromarray(arr[:, :, [2, 1, 0]])  # BGRA→RGB
        del arr
        return img.convert("RGB")  # 确保返回 RGB 模式
