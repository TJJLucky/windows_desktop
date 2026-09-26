"""QQ 业务组合函数：窗口截图、快捷键唤起、QQ 进程/窗口枚举。

依赖 core + vision 子包，处于依赖链顶端，全部 import 均为单向向下。

职责分层：
- 进程/窗口枚举：找到 QQ 的所有进程与可见窗口；
- 快捷键唤起：有进程但无窗口时，按 Ctrl+Alt+X（打开/隐藏 QQ 所有窗口）唤起主面板，
  取代旧的托盘图标点击方案（已移除）；
- 窗口就绪分级校验（L1/L2/L3）：保证"每次操作前窗口可用"，且用户未动窗口时跳过昂贵校验。
"""

# os：路径拼接
import os
# time：等待系统动画/渲染
import time
# Path：路径对象
from pathlib import Path

# psutil：进程枚举（按进程名找 QQ.exe）
import psutil
# pygetwindow：窗口枚举与几何信息（left/top/right/bottom/isMaximized）
import pygetwindow as gw
# win32gui：窗口可见性/最小化状态查询
import win32gui
# win32process：取窗口所属进程 PID
import win32process
# PIL.Image：模式切换按钮模板加载
from PIL import Image

# WGC 截图单例 / DPI 查询
from ..core.screenshot import WGCCapture, getDPI
# 窗口形态整理（高度铺满 + 宽 50% + 靠左）/ 层级控制
from ..core.window import layout_window_left_half, set_window_z_pos
# 模板匹配（模式切换按钮）
from ..vision.matcher import find_template
# 快捷键发送与配置表（唤醒主面板）
from .hotkeys import send_hotkey, TOGGLE_QQ_WINDOWS

# 模板目录：本文件在 utils/qq/，上溯三级到项目根再进 templates/
_TEMPLATE_DIR = os.path.join(Path(__file__).parent.parent.parent, "templates")


def start_qq():
    """唤醒登录：调用 QQ 的协议 URL（tencent://）触发 QQ 启动"""
    os.startfile("tencent://")


def get_qq_pids():
    """获取所有 QQ.exe 进程的 PID 列表"""
    qq_pids = []
    # 遍历系统全部进程
    for proc in psutil.process_iter(["name"]):  # 遍历所有进程
        # 进程名不区分大小写匹配 qq.exe
        if proc.info["name"].lower() == "qq.exe":  # 匹配进程名
            qq_pids.append(proc.pid)  # 收集 PID
    return qq_pids


def get_qq_windows():
    """遍历所有可见窗口，通过 PID 匹配哪些属于 QQ"""
    qq_pids = get_qq_pids()  # 获取 QQ 所有 PID
    if not qq_pids:
        # 没有 QQ 进程：后续操作无从谈起，打印并返回空
        print("[FAIL] QQ.exe 未运行")
        return []

    qq_windows = []
    # 遍历系统所有顶层窗口
    for win in gw.getAllWindows():
        title = win.title.strip()  # 窗口标题
        # 原实现曾跳过无标题/不可见窗口（已注释掉）：托盘缩起时主窗口可能不可见，
        # 保留全部窗口再由上层按需过滤更稳
        # if not title or not win.visible:  # 跳过无标题或不可见
        #     continue
        # 取窗口所属进程 PID（通过窗口句柄查）
        _, pid = win32process.GetWindowThreadProcessId(win._hWnd)  # 获取窗口 PID
        # PID 属于 QQ → 计入 QQ 窗口
        if pid in qq_pids:  # PID 匹配则属于 QQ
            qq_windows.append(win)
            print(pid, win.title)

    print(f"[OK] QQ 可见窗口共 {len(qq_windows)} 个")
    return qq_windows


def get_qq_window_image(window=None):
    """捕获 QQ 主窗口的 WGC 后台截图，返回 (PIL.Image(RGB), window)"""
    # WGC 截图单例
    WGC = WGCCapture()
    # 未指定窗口时取主窗口
    if window is None:
        window = get_main_window()
    # 后台捕获窗口画面
    image = WGC.capture(window._hWnd)
    if image is None:
        print("[WGC]截图失败")
    return image, window


def match_switch_to_big():
    """匹配效率模式→经典模式切换按钮，返回 {x,y,left,top,right,bottom} 或 None"""
    # 截取主窗口
    image, _ = get_qq_window_image()
    # 只保留顶部 40px（切换按钮在标题栏区域）
    image = image.crop((0, 0, image.width, 40))
    # 加载切换按钮模板
    template_path = os.path.join(_TEMPLATE_DIR, 'switch_to_big.png')
    small_image = Image.open(template_path)
    # 匹配（阈值 1.0 严格要求）
    region = find_template(image, small_image, 1)
    if not region:
        return None
    # 取第一个 QQ 窗口
    window = get_qq_windows()[0]
    # pygetwindow 和 WGC 截图坐标都在物理像素空间，无需 /scale
    # 截图内坐标 + 窗口屏幕原点 = 屏幕坐标
    return {"x": int(window.left + region["x"]), "y": int(window.top + region["y"]),
            "left": int(window.left + region["left"]), "top": int(window.top + region["top"]),
            "right": int(window.left + region["right"]), "bottom": int(window.top + region["bottom"])}


def match_switch_to_small():
    """匹配经典模式→效率模式切换按钮，返回 {x,y,left,top,right,bottom} 或 None"""
    # 截取主窗口
    image, _ = get_qq_window_image()
    # 顶部 40px 区域（切换按钮在标题栏）
    image = image.crop((0, 0, image.width, 40))
    # 加载切换按钮模板
    template_path = os.path.join(_TEMPLATE_DIR, 'switch_to_small.png')
    small_image = Image.open(template_path)
    # 匹配
    region = find_template(image, small_image, 1)
    if not region:
        return None
    # 取主窗口（与 match_switch_to_big 不同：这里用 get_main_window）
    window = get_main_window()
    # 截图内坐标 + 窗口屏幕原点 = 屏幕坐标
    return {"x": int(window.left + region["x"]), "y": int(window.top + region["y"]),
            "left": int(window.left + region["left"]), "top": int(window.top + region["top"]),
            "right": int(window.left + region["right"]), "bottom": int(window.top + region["bottom"])}


def get_main_window():
    """查找 QQ 主窗口（标题恰为 QQ），返回窗口对象或 None。"""
    # 遍历所有 QQ 窗口
    for win in get_qq_windows():
        # 标题（小写后）恰为 "qq" → 主窗口（会话窗口标题是联系人名，主窗口才是 qq）
        if win.title.strip().lower() == "qq":
            return win
    return None


def activate_qq(retry: int = 2) -> bool:
    """用快捷键唤起 QQ 主窗口（Ctrl+Alt+X：打开/隐藏 QQ 所有窗口），返回是否成功。

    细节与健壮性：
      - 主窗口已在 → 直接成功，不按快捷键（避免 toggle 反而隐藏窗口）。
      - 仅当完全无可见 QQ 窗口时才按快捷键（toggle 语义：有窗口时按=隐藏所有）。
      - 按后轮询确认主窗口出现，杜绝"按键成功却无窗口"的假阳性。
      - 唤起前提：QQ 进程已在运行（无进程时由 ensure_qq_window_with_retry 走 start_qq 登录唤起）。

    :param retry: 按快捷键后等待/重取主窗口的次数
    :return: 是否成功取得 QQ 主窗口
    """
    # 方案甲：主窗口已在 → 直接成功，不按快捷键
    if get_main_window() is not None:
        return True

    # 有可见 QQ 窗口但无主窗口（异常态）：toggle 会隐藏所有窗口，放弃本次唤起
    if get_qq_windows():
        print("[WARN] 存在可见 QQ 窗口但无主窗口，快捷键 toggle 会隐藏窗口，放弃唤起")
        return False

    # 无任何可见窗口（缩托盘/主面板已关闭）→ 按 Ctrl+Alt+X 打开所有窗口
    send_hotkey(TOGGLE_QQ_WINDOWS)
    # 等窗口弹出
    time.sleep(0.8)

    # 判空轮询：以主窗口出现为准，杜绝假阳性
    for _ in range(retry):
        if get_main_window() is not None:
            return True
        time.sleep(0.5)
    print("快捷键唤起后 QQ 主窗口未出现")
    return False


class QQWindowNotReadyError(RuntimeError):
    """QQ 主窗口在重试耗尽后仍未就绪（不静默降级）。"""


class _WindowState:
    """窗口就绪状态快照：跨调用复用，用户未动窗口时跳过昂贵校验。"""

    def __init__(self):
        # 快照对应的窗口句柄
        self.hwnd: int = 0
        # 快照时的窗口几何（left, top, right, bottom）
        self.rect: tuple[int, int, int, int] | None = None
        # 快照时的最大化状态（目标形态非最大化；用户手动最大化会被识别为变化并恢复）
        self.maximized: bool = False
        # 快照是否有效（首次校验成功后才为 True）
        self.valid: bool = False


# 模块级窗口状态快照（全局唯一）
_window_state = _WindowState()


def _window_l2_changed(win) -> bool:
    """L2 变更检测：窗口几何/最大化状态与快照不一致 = 用户动过窗口。"""
    # 快照无效或窗口句柄变了 → 视为变化（需要重新校验）
    if not _window_state.valid or _window_state.hwnd != win._hWnd:
        return True
    # 当前几何与快照不一致 → 用户移动/缩放过窗口
    rect = (win.left, win.top, win.right, win.bottom)
    if rect != _window_state.rect:
        return True
    # 最大化状态变化 → 用户切换过（目标形态应为非最大化）
    if bool(win.isMaximized) != _window_state.maximized:
        return True
    return False


def _snapshot_and_verify(win) -> bool:
    """L3 深度校验：置顶 + 形态整理 + WGC 试截，成功后更新快照。"""
    # 置顶（把窗口带到最前，不抢焦点）
    set_window_z_pos(win._hWnd)
    # 整理窗口形态：工作区高度铺满 + 宽 50% + 靠左（取代最大化，保证控件完整在屏内可点）
    layout_window_left_half(win._hWnd)
    # 等窗口重绘
    time.sleep(0.2)
    # WGC 试截：能截到画面 = 窗口真实可截图（不是黑块/缩略图）
    image, _ = get_qq_window_image(win)
    if image is None:
        # 试截失败 → 窗口仍不可用，不更新快照
        return False
    # 校验通过 → 更新快照（下次 L2 直接命中复用）
    _window_state.hwnd = win._hWnd
    _window_state.rect = (win.left, win.top, win.right, win.bottom)
    _window_state.maximized = bool(win.isMaximized)
    _window_state.valid = True
    return True


def ensure_qq_window():
    """对外统一入口：保证 QQ 主窗口处于前置、左半铺满（高度占满+宽50%+靠左）、可截图状态。

    分级校验（成本从低到高）：
      L1（每次）   主窗口存在（get_main_window 枚举可见窗口）。
      L2（快照比对）窗口几何/形态与快照一致 → 用户未动 → 直接复用，
                   跳过置顶/形态整理/试截等昂贵动作。
      L3（仅变化时）置顶 + 形态整理（高度铺满/宽 50%/靠左）+ WGC 试截确认可截图，
                   成功后更新快照。窗口最小化等同状态变化，走 L3 恢复。

    :return: 主窗口对象；失败返回 None（由 with_retry 决定重试/抛错）
    """
    # L1：取主窗口；不存在则尝试唤起
    main_win = get_main_window()
    if main_win is None and activate_qq():
        # 唤起成功后重取
        main_win = get_main_window()
    if main_win is None:
        return None
    # 最小化或几何变化（用户动过）→ L3 深度校验；否则 L2 命中直接复用
    if win32gui.IsIconic(main_win._hWnd) or _window_l2_changed(main_win):
        if not _snapshot_and_verify(main_win):
            return None
    return main_win


def ensure_qq_window_with_retry(retry: int = 3):
    """QQ 窗口就绪保证（带重试，默认每秒检查一次，最多等待约 2 秒）。

    流程：
      1. 无 QQ 进程 —— 首次尝试调用 start_qq() 唤起登录，随后继续轮询，
         给 QQ 启动或用户登录留出时间；后续重试不重复启动。
      2. 有 QQ 进程（已登录/运行中）—— 走 ensure_qq_window 分级校验
         （用户未动窗口时直接复用快照，省去置顶/形态整理/试截），失败重试。
      3. 重试耗尽仍未就绪 —— 抛 QQWindowNotReadyError，保证每次操作
         要么成功要么显式失败，不静默降级为空数据。

    :return: 主窗口对象；重试耗尽时抛 QQWindowNotReadyError
    """
    # start_qq 只触发一次；协议唤起是异步的，后续轮次负责重新探测进程。
    start_requested = False
    last_err: Exception | None = None
    # 循环重试
    for i in range(retry):
        # 无 QQ 进程：首次尝试唤起登录，后续等待
        if not get_qq_pids():
            if not start_requested:
                print("[INFO] 未检测到 QQ 进程，唤起登录（需用户手动登录/扫码）")
                start_qq()
                start_requested = True
            else:
                print(f"第 {i + 1} 次检查仍未检测到 QQ 进程，继续等待...")
            # 非最后轮次则等 1 秒再查
            if i < retry - 1:
                time.sleep(1)
            continue

        # 有 QQ 进程：走分级校验
        try:
            win = ensure_qq_window()
        except Exception as e:  # L3 试截等异常同样转为重试
            last_err = e
            win = None
        if win is not None:
            # 窗口就绪 → 返回
            return win
        print(f"第 {i + 1} 次唤起 QQ 窗口失败，重试...")
        if i < retry - 1:
            time.sleep(1)
    # 重试耗尽：显式抛错（上层转 503），绝不返回空数据
    raise QQWindowNotReadyError("QQ 主窗口在多次重试后仍未就绪") from last_err
