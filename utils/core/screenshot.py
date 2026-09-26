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
# wintypes：Windows 原生类型（RECT/HWND 等）
from ctypes import wintypes
# numpy：把 DLL 写回的字节缓冲重塑为像素数组
import numpy as np
# PIL.Image：把像素数组转成图像对象（截图返回给上层做模板匹配/OCR）
from PIL import Image
# os：定位 wgc_capture.dll 的绝对路径
import os
# win32gui：窗口句柄有效性/可见性/置顶状态查询
import win32gui
# win32con：窗口常量（SW_SHOWMINIMIZED 等）
import win32con
# win32print：查询设备 DPI（LOGPIXELSX/Y）
import win32print


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
class WGCCapture:
    """WGC 后台截图单例：复用 DLL 句柄与像素缓冲区，避免重复分配内存。

    用法：WGCCapture().capture(hwnd) → PIL Image 或 None（窗口不可用）。
    """

    # 类级单例实例
    _instance = None

    def __new__(cls):
        # 单例：只创建一次实例，后续调用直接返回已有实例
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # 初始化标记：__init__ 只跑一次
            cls._instance._initialized = False
            # 像素缓冲区缓存（复用，避免频繁 malloc）
            cls._instance._buf_cache = None
            # 当前缓冲区最大容量
            cls._instance._max_buf_len = 0
        return cls._instance

    def __init__(self):
        # 已初始化过则跳过（单例的 __init__ 会被重复调用）
        if self._initialized:
            return

        # DLL 路径：本文件在 utils/core/，上溯两级到项目根再进 libs/
        dll_path = os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", "..", "libs", "wgc_capture.dll"))  # 深一层
        # DLL 缺失直接抛错（打包/分发漏文件时第一时间暴露）
        if not os.path.exists(dll_path):
            raise FileNotFoundError(f"DLL not found: {dll_path}")
        # 加载 DLL
        self._dll_handle = ctypes.CDLL(dll_path)
        # 别名，方便外部直接调用 DLL 函数
        self.WGCDLL = self._dll_handle

        # 声明 DLL 导出函数签名: int WgcSnapshot(HWND, uint8_t*, int, int*, int*)
        # （参数：窗口句柄、输出缓冲、缓冲字节数、输出宽指针、输出高指针）
        self.WGCDLL.WgcSnapshot.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        # 返回值为 int（1=成功，其他=失败）
        self.WGCDLL.WgcSnapshot.restype = ctypes.c_int
        # 标记初始化完成
        self._initialized = True

    @classmethod
    def release(cls):
        """仅重置内存缓存与实例标记，不再手动卸载DLL"""
        # 有实例时清空缓存与标记，下次调用会重新初始化（DLL 交给进程退出时系统卸载）
        if cls._instance is not None:
            cls._instance._buf_cache = None
            cls._instance._max_buf_len = 0
            cls._instance._initialized = False
            cls._instance = None

    def _get_reuse_buffer(self, need_size: int):
        """缓冲区复用，减少频繁内存分配"""
        # 缓存不存在或容量不足时才重新分配；够用则直接复用旧缓冲区
        if self._buf_cache is None or need_size > self._max_buf_len:
            self._buf_cache = (ctypes.c_uint8 * need_size)()
            self._max_buf_len = need_size
        return self._buf_cache

    def capture(self, hwnd: int) -> Image.Image | None:
        """捕获指定窗口画面：返回 RGB 图像；窗口不可用返回 None。"""
        # 窗口有效性校验：不存在或不可见 → 无画面可截
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return None
        # 禁止最小化窗口捕获：最小化时窗口不渲染内容（WGC 拿不到画面）
        placement = win32gui.GetWindowPlacement(hwnd)
        if placement[1] == win32con.SW_SHOWMINIMIZED:
            return None

        # 计算所需缓冲区字节数；返回 0 表示窗口尺寸无效
        buf_size, *_ = calc_buf_size(hwnd)
        if buf_size <= 0:
            return None

        # 取（或分配）足够大的复用缓冲区
        buf = self._get_reuse_buffer(buf_size)
        # 输出宽/高指针（DLL 回填实际像素尺寸）
        out_w = ctypes.c_int(0)
        out_h = ctypes.c_int(0)

        # 调用 DLL 捕获：成功返回 1
        ret = self.WGCDLL.WgcSnapshot(
            hwnd,
            buf,
            buf_size,
            ctypes.byref(out_w),
            ctypes.byref(out_h)
        )

        # 捕获失败（窗口被遮挡/后台暂停渲染等）返回 None，调用方自行降级
        if ret != 1:
            return None

        # 回填的实际物理宽高
        w = out_w.value
        h = out_h.value
        # 像素字节数 = 宽 × 高 × 4（BGRA）
        pixel_len = w * h * 4
        # 把字节缓冲转为 uint8 数组并截取有效像素长度
        arr = np.frombuffer(buf, dtype=np.uint8)[:pixel_len]
        # 重塑为 (h, w, 4) 像素矩阵
        arr = arr.reshape(h, w, 4)

        # DLL 输出 BGRA 顺序 → 交换通道为 RGB（numpy 索引 [2,1,0] 把 B 和 R 对调）
        img = Image.fromarray(arr[:, :, [2, 1, 0]])  # BGRA→RGB
        # 释放数组（大缓冲区尽早还给解释器）
        del arr
        # 统一转成 RGB 模式返回（保证上层拿到一致格式）
        return img.convert("RGB")  # 确保返回 RGB 模式
