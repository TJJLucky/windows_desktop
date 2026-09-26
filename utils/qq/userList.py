"""QQ 用户列表模块：头像检测、用户行裁切、QQUser 数据模型、列表解析。"""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from collections import Counter
from dataclasses import dataclass
import time
import re

import cv2
import numpy as np
from PIL import Image

try:
    from .regions import get_userList_region_and_image, RegionResult
    from .composites import ensure_qq_window_with_retry
    from ..core.windows import WindowCaptureCtx
    from ..vision.ocr import OCREngine
    from ..core.mouse import random_click
    from ..core.windows import timer
except ImportError:
    from utils.qq.regions import get_userList_region_and_image, RegionResult
    from utils.qq.composites import ensure_qq_window_with_retry
    from utils.core.windows import WindowCaptureCtx
    from utils.vision.ocr import OCREngine
    from utils.core.mouse import random_click
    from utils.core.windows import timer


# ── 数据模型 ────────────────────────────────────────────────────────
@dataclass
class QQUser:
    """QQ 好友数据模型，含截图坐标、屏幕坐标换算与点击方法。"""

    # 文字信息
    name: str = ""  # 好友昵称/备注

    # 列表内行区域坐标 (left, top, right, bottom) rect为截图内坐标
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)

    # 头像圆心 + 半径 (cx, cy, r)
    avatar: tuple[int, int, int] = (0, 0, 0)

    active: bool = False
    new_msg: bool = False  # 是否有未读消息红点

    def setAvatar(self, avatar: tuple[int, int, int]) -> "QQUser":
        """设置头像圆心 + 半径，返回 self 支持链式调用。"""
        self.avatar = avatar
        return self

    def setRect(self, rect: tuple[int, int, int, int]) -> "QQUser":
        """设置列表内行区域坐标，返回 self 支持链式调用。"""
        self.rect = rect
        return self

    def setActive(self, active: bool):
        self.active = active
        return self

    def setName(self, name: str) -> "QQUser":
        """设置昵称，返回 self 支持链式调用。"""
        self.name = name
        return self

    def setNewMsg(self, new_msg: bool) -> "QQUser":
        self.new_msg = new_msg
        return self

    def to_dict(self) -> dict:
        """返回 QQUser 的可序列化完整信息（name/avatar/rect/active/new_msg）。"""
        return {
            "name": self.name,
            "avatar": list(self.avatar),  # (cx, cy, r)
            "rect": list(self.rect),  # (left, top, right, bottom)
            "active": self.active,
            "new_msg": self.new_msg,
        }


# ── 用户列表管理器 ──────────────────────────────────────────────────
class UserList:
    """QQ 好友列表管理器（单例）。

    持有列表截图，用户按坐标从截图中按需截取以判断状态。
    刷新时更新图片并重解析坐标，差异比对追踪变化。
    """

    _instance: "UserList | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self.users: dict[str, QQUser] = {}
        self.userList_region: RegionResult | None = None
        self.hwnd: int = 0

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
        arr = np.array(img)
        # 剔除干扰像素
        dark_text = np.all(arr < 45, axis=-1)  # 黑色文字/图标
        gray_text = np.all((arr >= 123) & (arr <= 183), axis=-1)  # 灰色昵称/签名
        shadow = np.all(arr < 200, axis=-1)  # 头像边缘阴影
        noise_mask = dark_text | gray_text | shadow
        bg_pixels = arr[~noise_mask]

        if len(bg_pixels) < 150:  # 有效背景像素太少，兜底
            return False

        # 最高频 RGB 即主体背景色
        most_common_rgb = Counter(tuple(px) for px in bg_pixels).most_common(1)[0][0]
        r, g, b = most_common_rgb

        # 区间容错判定
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
        target = np.array([247, 76, 48])  # QQ 红点标准色
        tolerance = 18  # 颜色容差（遮盖/抗锯齿）
        lower = np.clip(target - tolerance, 0, 255)
        upper = np.clip(target + tolerance, 0, 255)
        mask = cv2.inRange(img_rgb, lower, upper)  # 二值掩膜
        # 形态学开运算去噪
        kernel = np.ones((2, 2), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        # 调试：保存红点检测掩膜
        # UserList.save_debug(mask, "red_dot_mask")
        # 轮廓面积过滤（白色区域即命中，排除噪点和大块误检）
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = cv2.contourArea(c)
            if 200 <= area <= 400:  # 红点典型面积（下限 4px² 适配小分辨率）
                return True
        return False

    @staticmethod
    def save_debug(img: Image.Image, name: str = ""):
        """独立保存调试图片到 debug/ 目录，不影响主流程。"""
        from pathlib import Path
        import uuid
        debug_dir = Path(__file__).parent.parent.parent / "debug"
        debug_dir.mkdir(exist_ok=True)
        filename = f"{name}_{uuid.uuid4().hex[:6]}.png" if name else f"{uuid.uuid4().hex[:8]}.png"
        if isinstance(img, np.ndarray):
            img = Image.fromarray(img.astype(np.uint8))
        img.save(str(debug_dir / filename))

    @staticmethod
    def detect_avatar_circles(list_img: Image.Image) -> list[tuple]:
        """
        检测QQ好友列表里圆形头像，返回所有圆心坐标、半径
        :param list_img: 列表区域截图
        :return: list[(cx, cy, r)]
        """
        img = np.array(list_img)
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        circles = cv2.HoughCircles(
            gray,
            method=cv2.HOUGH_GRADIENT,
            dp=1,
            minDist=55,
            param1=60,
            param2=28,
            minRadius=18,
            maxRadius=35,
        )

        if circles is None:
            return []

        circles = np.uint16(np.around(circles))
        circle_list = []
        for i in circles[0, :]:
            cx, cy, r = int(i[0]), int(i[1]), int(i[2])
            circle_list.append((cx, cy, r))

        circle_list.sort(key=lambda x: x[1])
        return circle_list

    @staticmethod
    def crop_user_row_by_avatar(list_img: Image.Image) -> list[tuple[int, int, int, int]]:
        """以头像圆心为中心，用圆半径 + 5px 计算每个用户行区域。

        :param list_img: 列表区域截图
        :return: [(left, top, right, bottom), ...] 按从上到下排序
        """
        w, h = list_img.size
        circles = UserList.detect_avatar_circles(list_img)

        rects = []
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
        arr = np.array(img)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        h, w = gray.shape
        gray = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
        # ≤140 保留为黑字，>140 置白
        out = np.full_like(gray, 255)
        out[gray <= 140] = 0
        # 反转：白底黑字 → 黑底白字（OCR 更佳）
        binary = cv2.bitwise_not(out)
        kernel = np.ones((1, 1), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)  # 填补笔画断裂
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)  # 剔除零散噪点
        return binary

    @staticmethod
    @timer
    def ocr_recognize(img: Image.Image) -> str:
        """传入裁切好的文字区域图片，返回识别文本（只保留中文和英文字母）。"""
        proc = UserList.ocr_preprocess(img)  # 图像预处理
        result = OCREngine().ocr(proc)
        texts = []
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
        if self.userList_region is None or self.userList_region.image is None:
            raise ValueError("userList_region is required (call refresh() first)")
        if avatar is not None:
            cx, cy, r = avatar
            left, top, right, bottom = rect
            return self.userList_region.image.crop((cx + r + 5, top, right, bottom))
        return self.userList_region.image.crop(rect)

    @timer
    def active_user(self, user: QQUser):
        """激活指定用户（若非当前激活）。
        """
        if user.active:
            return
        if self.userList_region is None or self.hwnd == 0:
            raise RuntimeError("需要先执行refresh()")
        with WindowCaptureCtx(self.hwnd):
            offset_left = self.userList_region.screen_region["left"]
            offset_top = self.userList_region.screen_region["top"]
            rect_left, rect_top, rect_right, rect_bottom = user.rect
            random_click(
                offset_left + rect_left,
                offset_top + rect_top,
                rect_right - rect_left,
                rect_bottom - rect_top,
            )

    def find_user(self, contact_name: str) -> "QQUser | None":
        """按名字查找用户：直接使用包含匹配（OCR 名字可能截断/多符号），精确匹配天然被包含覆盖。

        返回第一个匹配的用户，找不到返回 None。
        """
        if not contact_name:
            return None
        # 双方都只保留中英文再比较：OCR 名字已移除符号（如 D_Isaac → DIsaac），
        # 输入的名字可能带下划线等符号，需同样规范化后才能命中。
        norm_contact = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", contact_name)
        if not norm_contact:
            return None
        for name, user in self.users.items():
            norm_name = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", name)
            if norm_contact in norm_name or norm_name in norm_contact:
                return user
        return None

    @timer
    def active_user_by_name(self, contact_name: str):
        self.refresh()
        user = self.find_user(contact_name)
        if user is None:
            raise ValueError(f"用户列表中没有找到与「{contact_name}」匹配的联系人")
        self.active_user(user)

    # ── 刷新 ────────────────────────────────────────────────────
    # 刷新前要调用
    @timer
    def refresh_image(self):
        """图片和句柄的刷新"""
        main_window = ensure_qq_window_with_retry()
        if main_window is None:
            print("[WARN] 无法获取 QQ 主窗口，跳过刷新")
            return
        self.userList_region = get_userList_region_and_image(main_window)
        self.hwnd = main_window._hWnd

    @timer
    def refresh(self):
        """全量重建用户列表，整体替换 self.users。"""
        self.refresh_image()
        new_users: dict[str, QQUser] = {}
        _, active_name = self.get_user_list_top_right_ocr()  # 顶部区域 OCR：active 用户的名字

        for avatar, rect in self.crop_user_row_by_avatar(self.userList_region.image):
            image = self.get_user_image(rect, avatar)  # 裁切：排除头像，保留文字+红点区域
            name = UserList.ocr_recognize(image)  # 阈值140 OCR 提取黑色昵称
            active = bool(name) and (name in active_name)  # 包含匹配：列表名字较短，是顶部 OCR 结果的子串
            new_msg = UserList.check_new_msg(np.array(image))  # RGB(247,76,48) 红点检测
            new_users[name] = QQUser().setName(name).setAvatar(avatar).setRect(rect).setActive(active).setNewMsg(
                new_msg)

        self.users = new_users
        self.clear_user_list_cache()

    def to_dict(self) -> dict[str, dict]:
        """返回用户列表的完整可序列化信息 {name: QQUser.to_dict()}。"""
        return {
            name: u.to_dict()
            for name, u in self.users.items()
        }

    def get_user_list_top_right(self, width: int = 500, height: int = 60):
        """以 userlist 图片右上角为区域左下角，向右 width、向上 height 截取外侧区域。"""
        region = self.userList_region
        if region is None or region.full_image is None or region.image_region is None:
            raise ValueError("userList_region with full_image is required (call refresh() first)")

        r = region.image_region
        # userlist 右上角作为目标区域左下角：向右 width、向上 height
        left = r["right"]
        bottom = r["top"]
        right = left + width
        top = bottom - height

        return region.full_image.crop((left, top, right, bottom))

    def get_user_list_top_right_ocr(self, width: int = 500, height: int = 60) -> tuple[bool, str]:
        """OCR userlist 右上角外侧区域，返回 (是否有数据, 识别文本)。"""
        img = self.get_user_list_top_right(width, height)

        text = UserList.ocr_recognize(img)  # 复用：阈值140只保留黑色 → OCR
        return (bool(text), text)

    # 清除缓存
    def clear_user_list_cache(self):
        self.cache_ts = 0.0

    # 1s缓存
    def get_user_list(self):
        now = time.time()
        if self.userList_region is None:
            self.refresh()
            self.cache_data = self.to_dict().copy()
            self.cache_ts = now
            return self.cache_data.copy()

        if now - self.cache_ts < 1.0:
            return self.cache_data

        self.refresh()
        self.cache_data = self.to_dict().copy()
        self.cache_ts = now
        return self.cache_data.copy()


if __name__ == "__main__":
    userList = UserList()
    userList.refresh()
    # userList.active_user(list(userList.users.values())[1])
    # userList.refresh()
    print(list(userList.users.values())[3])
    userList.active_user(list(userList.users.values())[3])
