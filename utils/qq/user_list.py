"""QQ 会话列表模块：会话行定位/激活、行区域与激活态判定、列表快照。

核心流程（身份是会话名，不是 QQ 号）：
1. 确保 QQ 主窗口就绪并截图，识别"好友列表区域"（截图与区域仍供未读红点等图像判定用）；
2. 会话行的枚举、滚动查找与定位交给无障碍树（见 contact_lookup / accessibility）：
   行名由 UI Automation 直接报出，不截图、不做昵称 OCR；
3. 激活 = 无障碍树里点中目标行 → 轮询聊天区标题确认（见 contact_lookup.activate）；
4. 行区域裁切与激活态色带判定（is_active_bg）仍按图像做，供诊断接口使用。

对外接口：activate（按会话名激活）、get_user_list（读会话名单快照）、
refresh_image（只刷新截图与区域）。
"""

# time：区域稳定等待
import time

# cv2：头像圆检测（霍夫圆）
import cv2
# numpy：像素数组运算
import numpy as np
# PIL.Image：图像裁切/尺寸
from PIL import Image

# 列表区域识别（截图 + 锚点定位）
from .regions import get_userList_region_and_image, RegionResult
# 窗口就绪保证（重试）
from .window_ops import ensure_qq_window_with_retry
# 计时装饰器
from ..core.timing import timed_stage, timer


# ── 用户列表管理器 ──────────────────────────────────────────────────
class UserList:
    """QQ 会话列表管理器（单例）。

    持有会话列表截图与区域坐标（供未读红点/激活态等图像判定）；会话身份与定位
    交给无障碍树：activate 按会话名激活，get_user_list 输出当前名单快照。
    """

    # 类级单例实例
    _instance: "UserList | None" = None

    def __new__(cls):
        # 单例：只创建一次
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # 已初始化过则跳过（单例 __init__ 会被重复调用）
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        # 列表区域识别结果（截图 + 区域坐标）
        self.userList_region: RegionResult | None = None
        # 当前关联的 QQ 主窗口句柄
        self.hwnd: int = 0

    @staticmethod
    def background_pixels(img: Image.Image) -> np.ndarray:
        """剔除文字/图标/边缘光晕，仅保留行主体背景像素（深浅主题通用）。

        旧实现用 `arr < 200` 当阴影噪声，会把深色主题的整块背景（113/122 灰）
        全部丢弃，导致深色主题永远判不出激活行。这里把阴影限定为 183~200 的
        中间灰光晕，深色背景得以保留。
        """
        arr = np.array(img.convert("RGB"))
        dark_text = np.all(arr < 45, axis=-1)  # 黑色文字/图标
        gray_text = np.all((arr >= 123) & (arr <= 183), axis=-1)  # 灰色昵称/签名
        halo = np.all((arr >= 183) & (arr < 200), axis=-1)  # 头像/文字边缘光晕
        return arr[~(dark_text | gray_text | halo)]

    @staticmethod
    def mode_color(pixels: np.ndarray) -> tuple[tuple[int, int, int], int]:
        """取出现次数最多的 RGB 及次数；并列时首次出现的颜色优先。"""
        rgb = pixels.astype(np.uint32)
        colors = (rgb[:, 0] << 16) | (rgb[:, 1] << 8) | rgb[:, 2]
        values, first_indices, counts = np.unique(colors, return_index=True, return_counts=True)
        candidates = np.flatnonzero(counts == counts.max())
        winner = int(values[candidates[np.argmin(first_indices[candidates])]])
        return (winner >> 16, (winner >> 8) & 255, winner & 255), int(counts.max())

    @staticmethod
    def modal_color(img: Image.Image) -> tuple[int, int, int]:
        """行内主体背景色；背景像素全被判为噪声时退回整图众数。"""
        pixels = UserList.background_pixels(img)
        if pixels.size == 0:
            pixels = np.array(img.convert("RGB")).reshape(-1, 3)
        return UserList.mode_color(pixels)[0]

    @staticmethod
    def is_active_bg(img: Image.Image) -> bool:
        """按绝对色带判断单行是否为激活态（浅色与深色主题各一档）。

        浅色主题：激活 222~230 灰，未激活 241~249 灰。
        深色主题：激活约 100~118 灰，未激活约 119~135 灰。
        非中性灰背景一律按未激活处理。

        :param img: 裁切好的单行好友背景图（建议排除头像右侧区域）
        :return: True=激活, False=未激活
        """
        pixels = UserList.background_pixels(img)
        # 有效背景像素太少（图太花/误判）→ 兜底按未激活处理
        if len(pixels) < 150:
            return False
        (r, g, b), _ = UserList.mode_color(pixels)
        # 非中性灰（带色调）不作为纯背景判定依据
        if max(r, g, b) - min(r, g, b) > 3:
            return False
        value = (r + g + b) / 3
        if value >= 200:  # 浅色主题
            return 216 <= value <= 234
        return 100 <= value <= 118  # 深色主题

    @staticmethod
    def check_new_msg(img_rgb: np.ndarray) -> bool:
        """检测 QQ 未读红点 RGB(247, 76, 48)。

        :param img_rgb: 已裁切掉头像的单行画面 numpy RGB
        :return: True=有未读消息
        """
        # QQ 红点标准色
        target = np.array([247, 76, 48])  # QQ 红点标准色
        # 颜色容差（遮盖/抗锯齿）
        tolerance = 18  # 颜色容差（遮盖/抗锯齿）
        # 颜色上下界
        lower = np.clip(target - tolerance, 0, 255)
        upper = np.clip(target + tolerance, 0, 255)
        # 二值掩膜：范围内像素置白，其余置黑
        mask = cv2.inRange(img_rgb, lower, upper)  # 二值掩膜
        # 形态学开运算去噪（2x2 核：消除孤立噪点）
        kernel = np.ones((2, 2), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        # 调试：保存红点检测掩膜
        # UserList.save_debug(mask, "red_dot_mask")
        # 轮廓面积过滤（白色区域即命中，排除噪点和大块误检）
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            # 红点典型轮廓面积区间（下限 4px² 适配小分辨率）
            area = cv2.contourArea(c)
            if 200 <= area <= 400:  # 红点典型面积（下限 4px² 适配小分辨率）
                return True
        return False

    @staticmethod
    def save_debug(img: Image.Image, name: str = ""):
        """独立保存调试图片到 debug/ 目录，不影响主流程。"""
        # 路径/文件名工具延迟 import（只在调试时用）
        from pathlib import Path
        import uuid
        # 调试目录 = 项目根/debug
        debug_dir = Path(__file__).parent.parent.parent / "debug"
        # 目录不存在则创建
        debug_dir.mkdir(exist_ok=True)
        # 文件名：可选前缀 + 随机短串（防覆盖）
        filename = f"{name}_{uuid.uuid4().hex[:6]}.png" if name else f"{uuid.uuid4().hex[:8]}.png"
        # numpy 数组先转 PIL 图
        if isinstance(img, np.ndarray):
            img = Image.fromarray(img.astype(np.uint8))
        # 保存
        img.save(str(debug_dir / filename))

    @staticmethod
    def detect_avatar_circles(list_img: Image.Image) -> list[tuple]:
        """
        检测QQ好友列表里圆形头像，返回所有圆心坐标、半径
        :param list_img: 列表区域截图
        :return: list[(cx, cy, r)]
        """
        # 转 numpy
        img = np.array(list_img)
        # 转灰度
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        # 5x5 高斯模糊：平滑噪点，提高圆检测稳定性
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        # 霍夫圆检测：参数针对 QQ 头像尺寸（18~35px 半径）
        circles = cv2.HoughCircles(
            gray,
            method=cv2.HOUGH_GRADIENT,
            dp=1,          # 累加器分辨率 = 原图
            minDist=55,     # 圆心最小间距（防止同一头像检测出多圆）
            param1=60,      # Canny 高阈值
            param2=28,      # 累加器阈值（越低越容易检出）
            minRadius=18,   # 最小半径
            maxRadius=35,   # 最大半径
        )

        # 没检出任何圆
        if circles is None:
            return []

        # 取整（float → uint16）
        circles = np.uint16(np.around(circles))
        circle_list = []
        # 提取每个圆的 (cx, cy, r)
        for i in circles[0, :]:
            cx, cy, r = int(i[0]), int(i[1]), int(i[2])
            circle_list.append((cx, cy, r))

        # 按 y（纵向位置）排序 → 与列表显示顺序一致
        circle_list.sort(key=lambda x: x[1])
        return circle_list

    @staticmethod
    def crop_user_row_by_avatar(list_img: Image.Image) -> list[tuple[int, int, int, int]]:
        """以头像圆心为中心，用圆半径 + 5px 计算每个用户行区域。

        :param list_img: 列表区域截图
        :return: [(left, top, right, bottom), ...] 按从上到下排序
        """
        # 列表宽高
        w, h = list_img.size
        # 检测全部头像圆
        circles = UserList.detect_avatar_circles(list_img)

        rects = []
        # 每个头像生成一行区域：横向整宽（0~w），纵向以头像为中心 ±(r+5)
        for (cx, cy, r) in circles:
            top = max(0, cy - r - 5)
            bottom = min(h, cy + r + 5)
            rects.append(((cx, cy, r), (0, top, w, bottom)))

        return rects

    @timer
    def activate(self, contact_name: str) -> str:
        """按会话名激活会话（无障碍树定位 → 点击 → 聊天区标题复核）。

        身份是会话行在无障碍树里自报的名字，不走 QQ 号、也不做昵称 OCR。
        目标不在视口时由 contact_lookup 滚动查找；已经是当前会话时直接返回、不点击
        （真机实测点击已激活的行会切掉激活高亮）。
        """
        # 延迟导入：contact_lookup 依赖 accessibility（UIA/COM），保证本模块可独立加载。
        from .contact_lookup import activate

        return activate(contact_name)

    # ── 刷新 ────────────────────────────────────────────────────
    # 刷新前要调用
    @timer
    def refresh_image(self, stabilize: bool = False):
        """刷新图片和句柄（窗口不就绪时抛 QQWindowNotReadyError）。

        WGC 截图本身会等待并取得当前已渲染帧。对当前 QQ 版本的实测表明，
        窗口重新布局后不需要再固定等待 1.2 秒即可稳定定位用户列表，因此默认
        直接截图以减少 contacts/read 的延迟。保留 ``stabilize=True`` 兼容旧调用，
        仅在明确需要给 QQ 异步动画留出额外时间时启用。
        """
        # 每个阶段单独计时；外层 @timer 输出 refresh_image 总耗时。
        with timed_stage("refresh_image.ensure_window"):
            main_window = ensure_qq_window_with_retry()
        # 兼容旧调用：只有显式 stabilize=True 时才等待旧的 1.2 秒。
        with timed_stage("refresh_image.stabilize_wait"):
            if stabilize:
                time.sleep(1.2)
        # 识别好友列表区域（含截图）
        with timed_stage("refresh_image.user_list_region"):
            self.userList_region = get_userList_region_and_image(main_window)
        # 记录窗口句柄（激活用户时用）
        self.hwnd = main_window._hWnd

    def get_user_list(self, contact_name: str | None = None):
        """读取会话名单快照（无障碍树）：会话名 → {rect, texts, active, pinned, copyable, refused, unread, unreadCount}。

        指定 contact_name 时先滚动确保目标在列表里（找不到抛 ContactNotFoundError）。
        """
        from .contact_lookup import list_contacts

        return list_contacts(contact_name)

