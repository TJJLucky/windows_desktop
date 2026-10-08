"""QQ 用户列表模块：头像检测、用户行裁切、User 数据模型、列表解析。

核心流程（refresh）：
1. 确保 QQ 主窗口就绪并截图，识别"好友列表区域"；
2. 霍夫圆检测列表里的圆形头像 → 每个头像即一个用户行；
3. 以头像为中心裁出行区域（排除头像，保留文字与红点区）；
4. 图像预处理 + OCR 提取黑色昵称；顶部区域 OCR 判定当前激活会话；
5. RGB 颜色检测未读红点；
6. 组装 User 并整体替换 self.users（3s 缓存对外）。
"""

# Counter：统计高频像素颜色（背景色判定）
from collections import Counter
# time：3s 缓存时间戳
import threading
import time
# re：昵称规范化（只留中英文）与 OCR 文本清洗
import re

# cv2：红点检测（inRange/形态学/轮廓）、预处理
import cv2
# numpy：像素数组运算
import numpy as np
# PIL.Image：图像裁切/尺寸
from PIL import Image

# User 数据模型（链式 setter 组装）
from .models import User
# 列表区域识别（截图 + 锚点定位）
from .regions import get_userList_region_and_image, RegionResult
# 窗口就绪保证（重试）
from .window_ops import ensure_qq_window_with_retry
# 无焦点置顶上下文（激活用户时短暂置顶窗口）
from ..core.window import WindowCaptureCtx
# 计时装饰器
from ..core.timing import timed_stage, timer
# OCR 引擎单例
from ..vision.ocr import OCREngine
# 拼图 OCR：多行文字区拼接一次识别 + 按 y 边界切分文本（性能优化）
from ..vision.compose import compose_bubbles_to_one, split_ocr_by_bubble
# 随机点击（激活会话时点用户行）
from ..core.mouse import random_click


USER_LIST_CACHE_TTL_SECONDS = 3.0


class ContactNotFoundError(ValueError):
    """当前可见 QQ 用户列表中没有目标联系人的可公开业务错误。"""

    def __init__(self, contact_name: str) -> None:
        self.contact_name = contact_name
        super().__init__(f"当前可见 QQ 用户列表中未找到联系人「{contact_name}」")


# ── 用户列表管理器 ──────────────────────────────────────────────────
class UserList:
    """QQ 好友列表管理器（单例）。

    持有列表截图，用户按坐标从截图中按需截取以判断状态。
    刷新时更新图片并重解析坐标，差异比对追踪变化。
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
        # 用户字典：name → User
        self.users: dict[str, User] = {}
        # 列表区域识别结果（截图 + 区域坐标）
        self.userList_region: RegionResult | None = None
        # 当前关联的 QQ 主窗口句柄
        self.hwnd: int = 0
        # 最近一次 refresh 顶部 OCR 识别的会话名（保留用于调试和状态比对）
        self.last_active_name: str = ""
        # 用户列表短 TTL 缓存：并发请求共用一次正在执行的刷新。
        self._cache_lock = threading.RLock()
        self._cache_condition = threading.Condition(self._cache_lock)
        self._refreshing = False
        self._cache_generation = 0
        # 最近完成的一轮刷新失败信息；只向等待该轮的调用者传播，下一次新调用可重试。
        self._cache_error: BaseException | None = None
        self._cache_error_generation = 0
        self.cache_ts: float = 0.0
        self.cache_data: dict = {}

    @staticmethod
    def is_active_bg(img: Image.Image) -> bool:
        """判断好友条目背景：226 激活 / 245 未激活。

        过滤文字、边缘残留、抗锯齿光晕后，取高频像素为背景色，
        用区间容错判定。
        :param img: 裁切好的单行好友背景图（建议排除头像右侧区域）
        :return: True=激活, False=未激活
        """
        # 转 numpy 数组
        arr = np.array(img)
        # 剔除干扰像素：
        dark_text = np.all(arr < 45, axis=-1)  # 黑色文字/图标
        gray_text = np.all((arr >= 123) & (arr <= 183), axis=-1)  # 灰色昵称/签名
        shadow = np.all(arr < 200, axis=-1)  # 头像边缘阴影
        noise_mask = dark_text | gray_text | shadow
        # 剩余像素即"主体背景"
        bg_pixels = arr[~noise_mask]

        # 有效背景像素太少（图太花/误判）→ 兜底按未激活处理
        if len(bg_pixels) < 150:  # 有效背景像素太少，兜底
            return False

        # 最高频 RGB 即主体背景色
        most_common_rgb = Counter(tuple(px) for px in bg_pixels).most_common(1)[0][0]
        r, g, b = most_common_rgb

        # 区间容错判定：222~230 灰 = 激活态背景（QQ 深一点）；241~249 = 未激活（白一点）
        if 222 <= r <= 230 and 222 <= g <= 230 and 222 <= b <= 230:
            return True  # 激活
        if 241 <= r <= 249 and 241 <= g <= 249 and 241 <= b <= 249:
            return False  # 未激活
        return False  # 中间值偏亮，默认未激活

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

    @staticmethod
    def ocr_preprocess(img: Image.Image, scale: float = 1.25) -> np.ndarray:
        """仅保留黑色文字：灰度→放大→阈值140剔除灰字+背景→降噪。

        140 精准分界：黑字 0~130 | 灰字 153 | 背景 226/245
        """
        # 转 numpy
        arr = np.array(img)
        # 转灰度
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        # 放大 1.25 倍（小字 OCR 更稳；INTER_CUBIC 平滑放大）
        h, w = gray.shape
        gray = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
        # ≤140 保留为黑字，>140 置白（阈值精准区分黑字与灰字/背景）
        out = np.full_like(gray, 255)
        out[gray <= 140] = 0
        # 反转：白底黑字 → 黑底白字（OCR 更佳）
        binary = cv2.bitwise_not(out)
        # 1x1 闭运算：填补笔画断裂（极小核，不伤细节）
        kernel = np.ones((1, 1), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)  # 填补笔画断裂
        # 开运算：剔除零散噪点
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)  # 剔除零散噪点
        return binary

    @staticmethod
    @timer
    def ocr_recognize(img: Image.Image) -> str:
        """传入裁切好的文字区域图片，返回清洗后的 OCR 昵称。"""
        # 图像预处理（二值化放大）
        proc = UserList.ocr_preprocess(img)  # 图像预处理
        # 调用 OCR 引擎
        result = OCREngine().ocr(proc)
        texts = []
        # 提取每行文本
        for line in result:
            text = line[1].strip()
            if text:
                texts.append(text)
        # 有中文/英文时保持原有清洗规则；纯符号昵称（例如 QQ 用户名 "!"）则保留符号。
        return UserList._clean_ocr_name("".join(texts))

    def get_user_image(self, rect: tuple[int, int, int, int],
                       avatar: tuple[int, int, int] | None = None) -> Image.Image:
        """从当前列表截图按坐标裁取图片。

        :param rect: 裁切区域 (left, top, right, bottom)
        :param avatar: 头像圆心+半径 (cx, cy, r)，传入时排除头像从头像右边缘开始裁
        """
        # 必须先 refresh 过（有列表截图）
        if self.userList_region is None or self.userList_region.image is None:
            raise ValueError("userList_region is required (call refresh() first)")
        # 传入头像时：从头像右边缘 +5px 开始裁（排除头像，只留文字+红点）
        if avatar is not None:
            cx, cy, r = avatar
            left, top, right, bottom = rect
            return self.userList_region.image.crop((cx + r + 5, top, right, bottom))
        # 否则整区域裁
        return self.userList_region.image.crop(rect)

    @timer
    def active_user(self, user: User) -> bool:
        """激活指定用户，返回本次是否执行了点击。"""
        if user.active:
            return False
        if self.userList_region is None or self.hwnd == 0:
            raise RuntimeError("需要先执行 refresh()")
        region = self.userList_region
        if region.full_image is None:
            raise RuntimeError("缺少整窗截图（full_image）")
        image_region = region.image_region
        screen_region = region.screen_region
        # 截图坐标 -> 屏幕坐标：加上截图时窗口在屏幕上的原点。
        origin_x = int(screen_region["left"] - image_region["left"])
        origin_y = int(screen_region["top"] - image_region["top"])
        circles = UserList.detect_avatar_circles(region.full_image)
        list_circles = [
            circle
            for circle in circles
            if image_region["left"] <= circle[0] < image_region["right"]
            and circle[1] >= image_region["top"]
        ]
        if not list_circles:
            raise RuntimeError("整窗截图中未检测到列表头像圆")
        # 用户行坐标相对列表区域图；转换成整窗截图坐标后匹配同一行的头像圆。
        target_y = image_region["top"] + user.avatar[1]
        circle = min(list_circles, key=lambda item: abs(item[1] - target_y))
        tolerance = max(10, user.avatar[2])
        if abs(circle[1] - target_y) > tolerance:
            raise RuntimeError(f"未找到用户行对应的整窗头像圆: {user.name}")
        cx, cy, radius = circle
        click_left = origin_x + cx + radius + 5
        click_y = origin_y + cy
        click_width = max(20, min(120, origin_x + image_region["right"] - click_left - 2))
        # 保留置顶：无激活置顶只改 Z 序，不移动窗口、不抢焦点。
        with WindowCaptureCtx(self.hwnd):
            random_click(click_left, click_y - 3, click_width, 6)
        return True

    def find_user(self, contact_name: str) -> "User | None":
        """按名字查找用户：直接使用包含匹配（OCR 名字可能截断/多符号），精确匹配天然被包含覆盖。

        返回第一个匹配的用户，找不到返回 None。
        """
        # 空名字直接失败
        if not contact_name:
            return None
        # 精确 key 优先，避免包含匹配命中其他相似联系人。
        if contact_name in self.users:
            return self.users[contact_name]
        # 中文/英文联系人按规范化文本匹配；纯符号联系人（如 "!"）保留符号。
        norm_contact = self._normalise_name(contact_name)
        # 规范化后为空才表示请求名确实为空，不能参与包含匹配。
        if not norm_contact:
            return None
        # 遍历用户，双向包含匹配（任一方向命中即可）
        for name, user in self.users.items():
            norm_name = self._normalise_name(name)
            # OCR 空行不能作为通配符："" in 任意字符串都会成立。
            if not norm_name:
                continue
            if norm_contact in norm_name or norm_name in norm_contact:
                return user
        return None

    @staticmethod
    def _clean_ocr_name(name: str) -> str:
        """清洗 OCR 昵称：文字昵称去符号，纯符号昵称保留。"""
        raw = re.sub(r"\s+", "", name or "")
        if not raw:
            return ""
        text = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", raw)
        return text or raw

    @staticmethod
    def _normalise_name(name: str) -> str:
        """规范化联系人名；纯符号名保留，空名保持为空。"""
        return UserList._clean_ocr_name(name)

    def _panel_switched_to(self, contact_name: str) -> bool:
        """用列表右上角 OCR 判断当前会话名是否命中目标联系人。"""
        _, active_name = self.get_user_list_top_right_ocr()
        return self._name_matches(contact_name, active_name)

    @staticmethod
    def _name_matches(contact_name: str, active_name: str) -> bool:
        """规范化后做双向包含匹配，兼容 OCR 截断和符号差异。"""
        norm_contact = UserList._normalise_name(contact_name)
        norm_active = UserList._normalise_name(active_name)
        if not norm_contact or not norm_active:
            return False
        return norm_contact in norm_active or norm_active in norm_contact

    def _wait_panel_switched(self, contact_name: str, timeout: float = 4.0) -> str | None:
        """轮询右上角 OCR，返回确认到的完整名字；超时返回 None。"""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            # 每次重新截图，避免拿点击前的缓存图判断新面板状态。
            self.refresh_image(stabilize=False)
            _, active_name = self.get_user_list_top_right_ocr()
            if self._name_matches(contact_name, active_name):
                return active_name
            time.sleep(0.25)
        return None

    def _promote_user_name(self, old_name: str, full_name: str) -> str:
        """用右上角 OCR 得到的完整名字替换用户列表 key。"""
        full_name = self._normalise_name(full_name)
        if not full_name:
            return old_name
        user = self.find_user(old_name)
        if user is None:
            return full_name
        # 移除旧 key（包括 OCR 截断 key），再以完整名字建立唯一 key。
        for key in [key for key, value in self.users.items() if value is user]:
            self.users.pop(key, None)
        with self._cache_lock:
            user.name = full_name
            self.users[full_name] = user
            self.last_active_name = full_name
            # key 更新不影响列表几何；同步缓存，后续并发请求可以继续复用。
            self.cache_data = self.to_dict().copy()
            self.cache_ts = time.time()
        return full_name

    @timer
    def active_user_by_name(self, contact_name: str) -> str:
        """按名字激活会话，并用右上角 OCR 更新 canonical key。"""
        # send/read 每次都必须重新读取用户列表；并发请求合并同一次正在执行的刷新。
        self._ensure_user_list_fresh(force=True)
        user = self.find_user(contact_name)
        if user is None:
            # 联系人不在当前可见列表时直接返回；不再做第二次完整 OCR 刷新。
            raise ContactNotFoundError(contact_name)
        clicked = False
        if not user.active:
            clicked = self.active_user(user)
        full_name = self._wait_panel_switched(contact_name)
        if full_name is not None:
            canonical = self._promote_user_name(contact_name, full_name)
            if clicked:
                # 实际点击可能引起列表滚动，旧坐标不能继续复用。
                self.clear_user_list_cache()
            return canonical

        # 点击后顶部 OCR 未确认时重试一次，重新拿坐标后再次点击。
        self._ensure_user_list_fresh(force=True)
        user = self.find_user(contact_name)
        if user is None:
            raise ContactNotFoundError(contact_name)
        clicked = self.active_user(user)
        full_name = self._wait_panel_switched(contact_name)
        if full_name is not None:
            canonical = self._promote_user_name(contact_name, full_name)
            if clicked:
                self.clear_user_list_cache()
            return canonical
        raise RuntimeError(f"顶部 OCR 未确认已激活联系人: {contact_name}")

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

    @timer
    def refresh(self):
        """全量重建用户列表，整体替换 self.users。

        性能优化：所有用户行的"文字区"垂直拼成一张图 → 一次 OCR → 按 y 边界切分，
        取代"每行单独 OCR"（N 次模型推理 → 1 次推理，显著降低刷新耗时）。
        """
        # 1) 刷新截图与区域
        self.refresh_image()
        new_users: dict[str, User] = {}
        # 2) 顶部区域 OCR：拿到当前激活会话的用户名
        _, active_name = self.get_user_list_top_right_ocr()  # 顶部区域 OCR：active 用户的名字
        # 记录最近一次顶部 OCR 的会话名（active_user：顶部有名字时不做行背景兜底）
        self.last_active_name = active_name

        # 3) 逐头像行处理：先收集全部行（不立即 OCR）
        rows: list[tuple[tuple[int, int, int], tuple[int, int, int, int], Image.Image]] = []
        for avatar, rect in self.crop_user_row_by_avatar(self.userList_region.image):
            # 裁切：排除头像，保留文字+红点区域
            image = self.get_user_image(rect, avatar)  # 裁切：排除头像，保留文字+红点区域
            # 暂存该行（avatar, rect, 文字区图）
            rows.append((avatar, rect, image))

        # 拼图：所有行的文字区垂直拼接成一张大图（保持原始宽高不拉伸，靠左留白）
        composed, bounds = compose_bubbles_to_one([img for _, _, img in rows])
        # 整图预处理一次（阈值140只保留黑色昵称，剔除灰字/背景）
        proc = UserList.ocr_preprocess(composed)
        # 一次 OCR 识别全部昵称（单例引擎复用）
        lines = OCREngine().ocr(proc) or []
        # 按 y 边界把识别行切分回每个用户行（顺序与 rows 一致）
        raw_names = split_ocr_by_bubble(lines, bounds, scale=1.25)

        # 逐行组装 User（名字已从拼图 OCR 获得）
        for (avatar, rect, image), name in zip(rows, raw_names, strict=True):
            # 主拼图 OCR 识别到纯符号时原样保留；空结果直接跳过。
            name = self._clean_ocr_name(name)
            if not name:
                continue
            # 顶部 OCR 是当前会话的完整名字；当前行命中时直接提升为 canonical key.
            active = self._name_matches(name, active_name)
            key = self._normalise_name(active_name) if active else name
            # RGB(247,76,48) 红点检测
            new_msg = UserList.check_new_msg(np.array(image))  # RGB(247,76,48) 红点检测
            # 组装 User（链式 setter）
            new_users[key] = User().setName(key).setAvatar(avatar).setRect(rect).setActive(active).setNewMsg(
                new_msg)

        # 整体替换（旧列表直接丢弃），并刷新短 TTL 快照。
        self.users = new_users
        self.cache_data = self.to_dict().copy()
        self.cache_ts = time.time()

    def to_dict(self) -> dict[str, dict]:
        """返回用户列表的完整可序列化信息 {name: User.to_dict()}。"""
        return {
            name: u.to_dict()
            for name, u in self.users.items()
        }

    def get_user_list_top_right(self, width: int = 500, height: int = 60):
        """以 userlist 图片右上角为区域左下角，向右 width、向上 height 截取外侧区域。

        该区域显示当前激活会话的名字（在列表右侧的面板头部）。
        """
        region = self.userList_region
        # 需要完整窗口截图与区域坐标
        if region is None or region.full_image is None or region.image_region is None:
            raise ValueError("userList_region with full_image is required (call refresh() first)")

        r = region.image_region
        # userlist 右上角作为目标区域左下角：向右 width、向上 height
        left = r["right"]
        bottom = r["top"]
        right = left + width
        top = bottom - height

        # 从完整窗口截图中裁该区域
        return region.full_image.crop((left, top, right, bottom))

    def get_user_list_top_right_ocr(self, width: int = 500, height: int = 60) -> tuple[bool, str]:
        """OCR userlist 右上角外侧区域，返回 (是否有数据, 识别文本)。"""
        # 裁区域
        img = self.get_user_list_top_right(width, height)

        # 复用：阈值140只保留黑色 → OCR
        text = UserList.ocr_recognize(img)  # 复用：阈值140只保留黑色 → OCR
        return (bool(text), text)

    def clear_user_list_cache(self):
        """手动使短 TTL 缓存失效，下次普通查询会重新刷新。"""
        with self._cache_condition:
            self.cache_ts = 0.0

    def _ensure_user_list_fresh(self, force: bool = False) -> None:
        """刷新用户列表；并发调用共享同一轮成功结果或失败异常。"""
        with self._cache_condition:
            if not force and self.userList_region is not None and time.time() - self.cache_ts < USER_LIST_CACHE_TTL_SECONDS:
                return

            start_generation = self._cache_generation
            while self._refreshing:
                self._cache_condition.wait()
            # 只复用本调用等待期间完成的那一轮。失败时所有等待者得到同一个异常，
            # 而刷新结束后才进入的新调用不会被旧异常永久阻断，可以开始下一轮重试。
            if self._cache_generation > start_generation:
                if self._cache_error_generation == self._cache_generation and self._cache_error is not None:
                    raise self._cache_error
                return

            self._refreshing = True

        error: BaseException | None = None
        try:
            self.refresh()
        except BaseException as exc:
            error = exc
        finally:
            with self._cache_condition:
                self._cache_generation += 1
                self._cache_error = error
                self._cache_error_generation = self._cache_generation if error is not None else 0
                self._refreshing = False
                self._cache_condition.notify_all()

        if error is not None:
            raise error

    def get_user_list(self):
        """普通查询优先复用短 TTL 缓存；send/read 使用 force=True。"""
        self._ensure_user_list_fresh(force=False)
        return self.cache_data.copy()
