"""QQ 用户列表模块：头像检测、用户行裁切、User 数据模型、列表解析。

核心流程（refresh）：
1. 确保 QQ 主窗口就绪并截图，识别"好友列表区域"；
2. 霍夫圆检测列表里的圆形头像 → 每个头像即一个用户行；
3. 以头像为中心裁出行区域（排除头像，保留文字与红点区）；
4. 图像预处理 + OCR 提取黑色昵称；顶部区域 OCR 判定当前激活会话；
5. RGB 颜色检测未读红点；
6. 组装 User 并整体替换 self.users（1s 缓存对外）。
"""

# Counter：统计高频像素颜色（背景色判定）
from collections import Counter
# time：1s 缓存时间戳
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
from ..core.timing import timer
# OCR 引擎单例
from ..vision.ocr import OCREngine
# 拼图 OCR：多行文字区拼接一次识别 + 按 y 边界切分文本（性能优化）
from ..vision.compose import compose_bubbles_to_one, split_ocr_by_bubble
# 随机点击（激活会话时点用户行）
from ..core.mouse import random_click


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
        # 最近一次 refresh 顶部 OCR 识别的会话名（active_user 据此决定是否走行背景兜底）
        self.last_active_name: str = ""

        # 1s 缓存：时间戳 + 缓存数据
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
        """传入裁切好的文字区域图片，返回识别文本（只保留中文和英文字母）。"""
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
        # 只保留中文和英文字母，剔除 OCR 误识别的特殊符号/省略号
        return re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", "".join(texts).strip())

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
    def active_user(self, user: User):
        """激活指定用户（若非当前激活）。

        跳过点击的条件（任一命中即无需点击）：
          1. refresh 已判定 user.active：顶部面板 OCR 的当前会话名包含该用户昵称；
          2. 行背景色为激活态（is_active_bg = 226 灰）：直接看用户列表行自身的高亮，
             顶部 OCR 漏判/名字截断时的可靠兜底——"根据用户列表已激活的用户不用激活"。

        点击策略（可靠性优化）：
          - 点击区收窄到"头像右边缘 +5px"起的昵称区，宽度上限 120px，
            避开头像、行右侧时间/红点/滚动条等"行外"区域；
          - y 锁定行中线 ±3px，彻底避开上下相邻行的边界（防点错人）；
          - 保留昵称区内随机取点，模拟人工点击（避免每次都点同一点）。
        """
        # ① refresh 已判定为当前会话 → 无需点击
        if user.active:
            return
        # ② 不再用"行背景灰=已激活"兜底跳过点击：该判定在空白态/面板为游戏中心时
        #    会误判（列表行渲染成灰但右侧面板并非该会话），误跳过会导致面板切不过去。
        #    是否已激活一律以顶部 OCR（user.active）为准；宁可重复点击已激活行（无害），
        #    也不可跳过未激活行的点击（激活失败）。
        # 未 refresh 过则无法点击
        if self.userList_region is None or self.hwnd == 0:
            raise RuntimeError("需要先执行refresh()")
        # 不置顶直接点击：WindowCaptureCtx 置顶（TOPMOST）会触发 QQ 重绘/列表滚动，
        # 使截图坐标与点击时刻不一致（点偏导致激活失败）。窗口由外部保证在屏内可点击
        # （layout 左半铺满工作区），直接以截图坐标点击最可靠。
        # 屏幕坐标 = 列表区域屏幕原点 + 行内相对坐标
        offset_left = self.userList_region.screen_region["left"]
        offset_top = self.userList_region.screen_region["top"]
        rect_left, rect_top, rect_right, rect_bottom = user.rect
        # 行垂直中心（屏幕坐标）：y 锁定行中线，彻底避开上下相邻行的边界（点错人）
        center_y = offset_top + (rect_top + rect_bottom) // 2
        # 昵称区典型宽度上限（物理像素）：QQ 会话列表昵称紧贴头像右侧，
        # 通常 120px 内已覆盖；限制点击宽度避免随机点落到行右侧的时间/红点/滚动条等"行外"区域
        nickname_band = 120
        # 头像信息有效时：点击区收窄到"头像右边缘 +5px"起的昵称区，
        # 同时宽度不超过昵称带（120px），避开头像本身与行右侧的无响应区
        avatar_cx, _, avatar_r = user.avatar
        if avatar_r > 0:
            # 昵称区左缘（屏幕坐标）
            click_left = offset_left + avatar_cx + avatar_r + 5
            # 昵称区宽度 = min(整行剩余宽, 昵称带上限)，至少 20px（太窄则退化）
            click_width = max(20, min(rect_right - (avatar_cx + avatar_r + 5) - 2, nickname_band))
        else:
            # 头像信息缺失（防御）→ 退化为整行点击（旧行为）
            click_left = offset_left + rect_left
            click_width = rect_right - rect_left
        # 窄带随机点击：x 在文字带内随机（模拟人工），y 只在中线 ±3px 内随机（绝不越行）
        random_click(click_left, center_y - 3, click_width, 6)

    def find_user(self, contact_name: str) -> "User | None":
        """按名字查找用户：直接使用包含匹配（OCR 名字可能截断/多符号），精确匹配天然被包含覆盖。

        返回第一个匹配的用户，找不到返回 None。
        """
        # 空名字直接失败
        if not contact_name:
            return None
        # 双方都只保留中英文再比较：OCR 名字已移除符号（如 D_Isaac → DIsaac），
        # 输入的名字可能带下划线等符号，需同样规范化后才能命中。
        norm_contact = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", contact_name)
        # 规范化后为空（纯符号名）→ 无法匹配
        if not norm_contact:
            return None
        # 遍历用户，双向包含匹配（任一方向命中即可）
        for name, user in self.users.items():
            norm_name = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", name)
            if norm_contact in norm_name or norm_name in norm_contact:
                return user
        return None

    def _panel_switched_to(self, contact_name: str) -> bool:
        """点击后验证：顶部区域 OCR 的当前会话名是否命中目标联系人（规范化包含匹配）。

        可靠点击闭环的验证器：面板切换后顶部应显示目标会话名；
        若仍显示旧面板（如 QQ 游戏中心）说明点击未生效/点偏，需重试。
        """
        # 顶部区域 OCR：当前会话名（无数据时为空串）
        _, active_name = self.get_user_list_top_right_ocr()
        if not active_name:
            # 顶部无任何文字（漏判）→ 无法确认切换成功，视为未切换
            return False
        # 双方都只保留中英文再比较（与 find_user 同款规范化）
        norm_contact = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", contact_name)
        norm_active = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", active_name)
        # 空名字无法匹配
        if not norm_contact:
            return False
        # 双向包含匹配（OCR 可能截断/多符号）
        return norm_contact in norm_active or norm_active in norm_contact

    @timer
    def active_user_by_name(self, contact_name: str):
        """按名字激活会话：刷新列表 → 点击激活 → 验证切换，未成功则重试。

        可靠点击闭环：点击后验证右侧面板是否已切到目标会话（顶部 OCR），
        未切换则重新刷新坐标再点（最多 2 轮），防御列表滚动等偶发坐标漂移。
        """
        # 先刷新（保证列表最新）
        self.refresh()
        # 查找用户
        user = self.find_user(contact_name)
        if user is None:
            raise ValueError(f"用户列表中没有找到与「{contact_name}」匹配的联系人")
        # 点击激活
        self.active_user(user)
        # 可靠点击闭环：等待面板切换，未切换则重新刷新坐标再点（最多 2 轮）
        for _ in range(2):
            # 等 QQ 响应点击（面板切换/列表滚动就位需要时间）
            time.sleep(0.5)
            # 顶部 OCR 验证是否已切到目标会话
            if self._panel_switched_to(contact_name):
                return
            # 未切换：重新刷新（此时列表已稳定，拿最新坐标）再点一次
            self.refresh()
            user = self.find_user(contact_name)
            if user is not None:
                self.active_user(user)

    # ── 刷新 ────────────────────────────────────────────────────
    # 刷新前要调用
    @timer
    def refresh_image(self):
        """图片和句柄的刷新（窗口不就绪时抛 QQWindowNotReadyError）。"""
        # 确保窗口就绪（分级校验 + 重试）
        main_window = ensure_qq_window_with_retry()
        # 识别好友列表区域（含截图）
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
            # 清洗：只保留中文和英文字母（剔除 OCR 误识别的特殊符号/省略号）
            name = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", name.strip())
            # 包含匹配：列表名字较短，是顶部 OCR 结果的子串
            active = bool(name) and (name in active_name)  # 包含匹配：列表名字较短，是顶部 OCR 结果的子串
            # RGB(247,76,48) 红点检测
            new_msg = UserList.check_new_msg(np.array(image))  # RGB(247,76,48) 红点检测
            # 组装 User（链式 setter）
            new_users[name] = User().setName(name).setAvatar(avatar).setRect(rect).setActive(active).setNewMsg(
                new_msg)

        # 整体替换（旧列表直接丢弃）
        self.users = new_users
        # 清缓存：列表变了，1s 缓存作废
        self.clear_user_list_cache()

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

    # 清除缓存
    def clear_user_list_cache(self):
        # 时间戳归零 → 下次 get_user_list 强制刷新
        self.cache_ts = 0.0

    # 1s缓存
    def get_user_list(self):
        now = time.time()
        # 从未初始化 → 全量刷新并填充缓存
        if self.userList_region is None:
            self.refresh()
            self.cache_data = self.to_dict().copy()
            self.cache_ts = now
            return self.cache_data.copy()

        # 1s 内命中缓存 → 直接返回（避免高频轮询频繁视觉识别）
        if now - self.cache_ts < 1.0:
            return self.cache_data

        # 超过 1s → 刷新并更新缓存
        self.refresh()
        self.cache_data = self.to_dict().copy()
        self.cache_ts = now
        # 返回副本：调用方修改不污染缓存
        return self.cache_data.copy()
