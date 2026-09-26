"""QQ 业务组合函数：窗口截图、激活、托盘图标匹配、QQ 进程/窗口枚举。

依赖 core + vision 子包，处于依赖链顶端，全部 import 均为单向向下。
"""

import os
import time
from pathlib import Path

import psutil
import pygetwindow as gw
import win32gui
import win32process
from pywinauto import Desktop
from PIL import Image, ImageGrab

from ..core.screenshot import WGCCapture, getDPI
from ..core.window import maximize_window, set_window_z_pos
from ..core.mouse import random_click
from ..vision.matcher import find_template

_TEMPLATE_DIR = os.path.join(Path(__file__).parent.parent.parent, "templates")


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


def get_main_qq_windows():
    """通过关闭QQ窗口,再通过后台的图标打开,从而获得QQ唯一窗口"""
    close_all_qq_windows()
    click_qq_tray_icon()
    return get_qq_windows()[0]


def get_overflow():
    """截取任务栏托盘溢出窗口的屏幕截图 → PIL.Image

    用 ImageGrab 而非 WGC —— 系统托盘窗口受保护，WGC 无法捕获。
    """
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


def get_qq_window_image(window=None):
    """捕获 QQ 主窗口的 WGC 后台截图，返回 PIL.Image(RGB)"""
    WGC = WGCCapture()
    if window is None:
        window = get_main_window()
    image = WGC.capture(window._hWnd)
    if image is None:
        print("[WGC]截图失败")
    return image, window


def match_overflow_qq_icon():
    """匹配溢出区域的QQ图标，返回 {x,y,left,top,right,bottom} 或 None"""
    result = get_overflow()
    if not result:
        print("未找到溢出窗口")
        return None
    image, bbox = result
    template_path = os.path.join(_TEMPLATE_DIR, 'QQ_NotifyIconOverflowWindow.png')
    small_image = Image.open(template_path)
    region = find_template(image, small_image, 0.4, True)
    if not region:
        return None
    return {"x": bbox[0] + region["x"], "y": bbox[1] + region["y"],
            "left": bbox[0] + region["left"], "top": bbox[1] + region["top"],
            "right": bbox[0] + region["right"], "bottom": bbox[1] + region["bottom"]}


def match_switch_to_big():
    """匹配效率模式→经典模式切换按钮，返回 {x,y,left,top,right,bottom} 或 None"""
    image, _ = get_qq_window_image()
    image = image.crop((0, 0, image.width, 40))
    template_path = os.path.join(_TEMPLATE_DIR, 'switch_to_big.png')
    small_image = Image.open(template_path)
    region = find_template(image, small_image, 1)
    if not region:
        return None
    window = get_qq_windows()[0]
    # pygetwindow 和 WGC 截图坐标都在物理像素空间，无需 /scale
    return {"x": int(window.left + region["x"]), "y": int(window.top + region["y"]),
            "left": int(window.left + region["left"]), "top": int(window.top + region["top"]),
            "right": int(window.left + region["right"]), "bottom": int(window.top + region["bottom"])}


def match_switch_to_small():
    """匹配经典模式→效率模式切换按钮，返回 {x,y,left,top,right,bottom} 或 None"""
    image, _ = get_qq_window_image()
    image = image.crop((0, 0, image.width, 40))
    template_path = os.path.join(_TEMPLATE_DIR, 'switch_to_small.png')
    small_image = Image.open(template_path)
    region = find_template(image, small_image, 1)
    if not region:
        return None
    window = get_main_window()
    return {"x": int(window.left + region["x"]), "y": int(window.top + region["y"]),
            "left": int(window.left + region["left"]), "top": int(window.top + region["top"]),
            "right": int(window.left + region["right"]), "bottom": int(window.top + region["bottom"])}


def get_main_window():
    """查找 QQ 主窗口，返回窗口对象或 None。"""
    for win in get_qq_windows():
        if win.title.strip().lower() == "qq":
            return win
    return None


def activate_qq(retry: int = 2) -> bool:
    """唤起 QQ 主窗口，返回是否成功。

    细节与健壮性：
      - 开头判断：若已存在标题为 QQ 的主窗口 → 直接返回成功（不关闭不重建）。
      - 仅当确实存在可见 QQ 窗口时才关闭（按需），避免破坏性空操作。
      - 非视觉 click_qq_tray_icon 优先，失败再 match_overflow 视觉兜底。
      - 成功后按 retry 轮询确认主窗口已出现，杜绝点击成功却无窗口的假阳性。

    :param retry: 点击托盘后等待/重取主窗口的次数
    :return: 是否成功取得 QQ 主窗口
    """
    # 方案甲：主窗口已在 → 直接成功，不必关闭重建
    if get_main_window() is not None:
        return True

    # 仅在有可见 QQ 窗口时才关闭（缩托盘场景：关掉干净态再唤起）
    if get_qq_windows():
        close_all_qq_windows()

    # 展开托盘溢出区（失败不致命，视觉兜底会自行点击）
    clickExpandBtn()

    # 非视觉唤起优先，失败则视觉匹配兜底
    clicked = click_qq_tray_icon()
    if not clicked:
        print("非视觉唤起失败，改用图像匹配点击托盘 QQ 图标")
        region = match_overflow_qq_icon()
        if region is None:
            print("未识别到托盘 QQ 图标，唤起失败")
            return False
        random_click(
            region["left"], region["top"],
            region["right"] - region["left"],
            region["bottom"] - region["top"],
        )
    time.sleep(0.8)

    # 判空轮询：以主窗口出现为准，杜绝假阳性
    for _ in range(retry):
        if get_main_window() is not None:
            return True
        time.sleep(0.5)
    print("点击托盘后 QQ 主窗口未出现")
    return False


def ensure_qq_window():
    """对外统一入口：保证 QQ 主窗口处于前置、最大化、可截图状态。

    内部细节：
      - 已有主窗口 → 置顶 + 最大化后返回。
      - 无主窗口  → 经 activate_qq() 唤起后再取一次窗口并判空。

    :return: 主窗口对象，失败返回 None
    """
    main_win = get_main_window()
    if main_win is None and activate_qq():
        main_win = get_main_window()
    if main_win is not None:
        set_window_z_pos(main_win._hWnd)
        maximize_window(main_win._hWnd)
        time.sleep(0.2)
    return main_win


def ensure_qq_window_with_retry(retry: int = 3):
    """QQ 窗口就绪保证（带重试，默认每秒检查一次，最多等待约 2 秒）。

    流程：
      1. 无 QQ 进程 —— 首次尝试调用 start_qq() 唤起登录，随后继续轮询，
         给 QQ 启动或用户登录留出时间；后续重试不重复启动。
      2. 有 QQ 进程（已登录/运行中）—— 走 ensure_qq_window 唤起/保证窗口，
         失败则继续重试。

    :return: 主窗口对象；重试耗尽仍未就绪时返回 None
    """
    # start_qq 只触发一次；协议唤起是异步的，后续轮次负责重新探测进程。
    start_requested = False
    for i in range(retry):
        if not get_qq_pids():
            if not start_requested:
                print("[INFO] 未检测到 QQ 进程，唤起登录（需用户手动登录/扫码）")
                start_qq()
                start_requested = True
            else:
                print(f"第 {i + 1} 次检查仍未检测到 QQ 进程，继续等待...")
            if i < retry - 1:
                time.sleep(1)
            continue

        win = ensure_qq_window()
        if win is not None:
            return win
        print(f"第 {i + 1} 次唤起 QQ 窗口失败，重试...")
        if i < retry - 1:
            time.sleep(1)
    return None
