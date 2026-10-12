"""WGC 后台窗口截图与 DPI 工具。不依赖其他 utils 子包。

窗口像素只允许来自 ``wgc`` 对明确 HWND 的原生后台捕获。

为什么用 WGC（Windows Graphics Capture API 的 C 封装 DLL）而不是 PrintWindow/前台截图：
- 前台截图需要把窗口带到前台，会打断用户操作；WGC 可以在后台直接捕获指定窗口画面；
- 受保护/硬件加速渲染的窗口（QQ 消息区）用传统 GDI 截图经常拿到黑块，WGC 能拿到真实内容。

DPI 约定：进程启动时固定 per-monitor V2 DPI-aware，之后所有坐标与尺寸均为物理像素，
WGC 返回的像素尺寸即物理尺寸，与 GetWindowRect 在 DPI-aware 下一致。
"""

# ctypes：调用 Win32 API 与加载原生 DLL
import ctypes
from dataclasses import dataclass
import logging
import threading
import time
# wintypes：Windows 原生类型（RECT/HWND 等）
from ctypes import wintypes
# numpy：把 DLL 写回的字节缓冲重塑为像素数组
# PIL.Image：把像素数组转成图像对象（截图返回给上层做模板匹配/OCR）
from PIL import Image
# os：定位 wgc_capture.dll 的绝对路径
import os
# win32gui：窗口句柄有效性/可见性/置顶状态查询
import win32gui
# win32con：窗口常量（SW_SHOWMINIMIZED 等）
# win32print：查询设备 DPI（LOGPIXELSX/Y）
import win32print


logger = logging.getLogger("qq_service.capture")

# ── DPI ────────────────────────────────────────────────────────────
def ensure_dpi_aware():
    """启动时固定进程 DPI-aware（per-monitor），使 GetWindowRect/WGC/SetCursorPos 统一为物理像素。

    按优先级尝试三个 API（老系统逐个降级）：PER_MONITOR_AWARE_V2 → PER_MONITOR → SYSTEM_DPI_AWARE。
    """
    try:
        # 首选：per-monitor V2（-4 = PER_MONITOR_AWARE_V2），缩放随窗口所在显示器实时变化
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        return True
    except Exception:
        pass
    try:
        # 降级：per-monitor（老版本 Windows 10），不支持 V2 时用
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        return True
    except Exception:
        pass
    try:
        # 再降级：系统 DPI aware（Win8 及更早），统一按主显示器缩放
        ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:
        # 全部失败：进程保持"DPI 虚拟化"（最差情况，坐标会被系统换算）
        return False


def getDPI():
    """获取系统 DPI 缩放比例。96=100% 基准，如 144→1.5 倍。"""
    # 取桌面设备上下文（HDC）
    hdc = win32gui.GetDC(0)  # 桌面 DC
    # LOGPIXELSX(88)：水平方向每英寸像素数
    dpi_x = win32print.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
    # LOGPIXELSY(90)：垂直方向每英寸像素数
    dpi_y = win32print.GetDeviceCaps(hdc, 90)  # LOGPIXELSY
    # 释放设备上下文（防止句柄泄漏）
    win32gui.ReleaseDC(0, hdc)
    # 缩放比 = 实际 DPI / 基准 96
    DPI = dpi_x / 96.0
    print("[DPI]:", DPI)
    # 返回缩放比例（如 1.0 / 1.5 / 2.0）
    return DPI  # 缩放比例


def window_dpi(hwnd):
    """窗口所在显示器的实际 DPI（``GetDpiForWindow``）；取不到返回 0。

    进程已固定 per-monitor V2 DPI-aware，因此这就是该窗口当前物理像素与逻辑像素
    之比（96=100%，120=125%，144=150%，168=175%，192=200%...）。跨电脑判断模板、
    框选几何要不要按缩放比例放大时，以窗口自身所在显示器的 DPI 为准，而不是主屏。
    """
    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(int(hwnd)))
    except Exception:
        return 0
    return dpi if dpi > 0 else 0


def calc_buf_size(hwnd):
    """根据窗口逻辑尺寸 × 系统 DPI 缩放，计算 WGC 截图所需缓冲区字节数。

    GetWindowRect 在非 DPI-aware 进程返回 DPI 虚拟化坐标，
    WGC 返回物理像素。比值 = 系统 DPI 缩放系数。
    buf_size = logic_w × scale × logic_h × scale × 4 + 10% 安全余量。

    返回 (buf_size, logic_w, logic_h, scale)
    """
    # 取窗口矩形（物理像素，因为进程已 DPI-aware）
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    # 窗口逻辑宽高
    logic_w = rect.right - rect.left
    logic_h = rect.bottom - rect.top

    # 窗口无效（宽或高 ≤0）直接返回零值，调用方放弃本次捕获
    if logic_w <= 0 or logic_h <= 0:
        return 0, 0, 0, 0.0

    # 取系统缩放比例
    scale = getDPI()
    # 缓冲区 = 物理宽(逻辑宽×缩放) × 物理高 × 每像素4字节(BGRA) × 1.1 安全余量
    # （WGC 可能多写几个字节，余量防止越界读）
    buf_size = int(logic_w * scale * logic_h * scale * 4 * 1.1)  # 额外给1.1倍的空间
    return buf_size, logic_w, logic_h, scale


# ── WGC 捕获 ───────────────────────────────────────────────────────
class WgcFrameInfo(ctypes.Structure):
    """Native ABI; QPC and WGC times are in 100 ns units."""

    _fields_ = [("struct_size", ctypes.c_uint32), ("width", ctypes.c_uint32),
                ("height", ctypes.c_uint32), ("required_bytes", ctypes.c_uint32),
                ("generation", ctypes.c_uint64), ("frame_number", ctypes.c_uint64),
                ("frame_time_100ns", ctypes.c_int64), ("arrival_time_100ns", ctypes.c_int64),
                ("error", ctypes.c_int32), ("hresult", ctypes.c_int32)]


class WgcCaptureError(RuntimeError):
    def __init__(self, status, info=None):
        self.status = status
        self.info = info
        self.code = {-1: "WGC_INVALID_ARGUMENT", -2: "WGC_WINDOW_UNAVAILABLE",
                     -3: "WGC_TIMEOUT", -4: "WGC_BUFFER_TOO_SMALL", -5: "WGC_SESSION_CHANGED",
                     -6: "WGC_DEVICE_LOST", -7: "WGC_CAPTURE_FAILED", -8: "WGC_CLOSED"}.get(status, "WGC_CAPTURE_FAILED")
        super().__init__(self.code)


@dataclass(frozen=True)
class CapturedFrame:
    image: Image.Image
    info: WgcFrameInfo


class WGCCapture:
    """One persistent native session, serialized reads, independently owned RGB images."""

    _instance = None
    _instance_lock = threading.RLock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._lock = threading.RLock()
                instance._initialized = False
                cls._instance = instance
            return cls._instance

    def __init__(self):
        with self._lock:
            if self._initialized:
                return
            dll_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "libs", "wgc_capture.dll"))
            self.WGCDLL = ctypes.CDLL(dll_path)
            self.WGCDLL.WgcOpen.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_uint64)]
            self.WGCDLL.WgcRead.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint8),
                                           ctypes.c_uint32, ctypes.c_int64, ctypes.c_uint32,
                                           ctypes.POINTER(WgcFrameInfo)]
            self.WGCDLL.WgcClose.argtypes = [ctypes.c_uint64]
            self.WGCDLL.WgcClock100ns.argtypes = []
            self.WGCDLL.WgcClock100ns.restype = ctypes.c_int64
            for name in ("WgcOpen", "WgcRead", "WgcClose"):
                getattr(self.WGCDLL, name).restype = ctypes.c_int
            self._session = 0
            self._hwnd = None
            self._buf_cache = None
            self._max_buf_len = 0
            self._initialized = True

    def clock_100ns(self):
        return self.WGCDLL.WgcClock100ns()

    def _close(self):
        if self._session:
            self.WGCDLL.WgcClose(self._session)
        self._session = 0
        self._hwnd = None
        self._buf_cache = None
        self._max_buf_len = 0

    @classmethod
    def release(cls):
        """Idempotent; never creates an unused capture engine during shutdown."""
        with cls._instance_lock:
            if cls._instance is not None:
                with cls._instance._lock:
                    cls._instance._close()

    def _get_reuse_buffer(self, need_size):
        if need_size > self._max_buf_len:
            self._buf_cache = (ctypes.c_uint8 * need_size)()
            self._max_buf_len = need_size
        return self._buf_cache

    def capture_frame(self, hwnd, *, after_100ns=None, timeout_ms=300):
        """Return a fresh frame or an explicit error. Never return an old frame on timeout."""
        with self._lock:
            started = time.perf_counter()
            if not 0 <= timeout_ms <= 300:
                raise ValueError("WGC read timeout must be between 0 and 300 ms")
            barrier = max(self.clock_100ns(), after_100ns or 0)
            if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                self._close()
                raise WgcCaptureError(-2)
            if self._hwnd != hwnd:
                self._close()
            session = ctypes.c_uint64()
            status = self.WGCDLL.WgcOpen(hwnd, ctypes.byref(session))
            if status != 1:
                self._close()
                raise WgcCaptureError(status)
            self._session, self._hwnd = session.value, hwnd
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            need = max(4, (right - left) * (bottom - top) * 4)
            buffer = self._get_reuse_buffer(need)
            deadline = time.perf_counter() + timeout_ms / 1000
            info = WgcFrameInfo()
            while True:
                info.struct_size = ctypes.sizeof(WgcFrameInfo)
                remaining = max(0, int((deadline - time.perf_counter()) * 1000))
                status = self.WGCDLL.WgcRead(self._session, buffer, self._max_buf_len,
                                             barrier, remaining, ctypes.byref(info))
                if status != -4:
                    break
                if info.required_bytes <= self._max_buf_len or time.perf_counter() >= deadline:
                    raise WgcCaptureError(status, info)
                buffer = self._get_reuse_buffer(info.required_bytes)
            duration = (time.perf_counter() - started) * 1000
            logger.info("capture.frame status=%s generation=%s frame=%s wgc_time=%s arrival_time=%s barrier=%s duration_ms=%.2f",
                        status, info.generation, info.frame_number, info.frame_time_100ns,
                        info.arrival_time_100ns, barrier, duration)
            if status != 1:
                if status in (-2, -6, -7, -8):
                    self._close()
                raise WgcCaptureError(status, info)
            if info.frame_time_100ns <= barrier or info.arrival_time_100ns <= barrier:
                raise WgcCaptureError(-3, info)
            if (not info.width or not info.height or info.required_bytes != info.width * info.height * 4
                    or info.required_bytes > self._max_buf_len):
                raise WgcCaptureError(-7, info)
            # frombytes owns its data: future native reads cannot overwrite this image.
            image = Image.frombytes("RGB", (info.width, info.height), ctypes.string_at(buffer, info.required_bytes), "raw", "BGRX")
            logger.info("capture.image duration_ms=%.2f", (time.perf_counter() - started) * 1000)
            return CapturedFrame(image, info)

    def capture(self, hwnd) -> Image.Image | None:
        """Existing screenshot contract; diagnostic detail is logged by capture_frame."""
        try:
            return self.capture_frame(hwnd).image
        except WgcCaptureError as error:
            logger.warning("capture.failed code=%s", error.code)
            return None
