"""QQ 消息框模块：气泡检测、裁切、发送方判断。

MessageList（单例）：
1. 截取消息区域（裁剪掉左右头像带）；
2. 颜色替换预处理：把难区分的浅色（背景/对方白泡/我方浅蓝泡）换成"黑/红/白"三色；
3. 轮廓检测提取每个气泡的外包围盒（每行去重）；
4. 按"气泡中心在左/右半区"判定发送方（我方靠右，对方靠左）；
5. 逐气泡裁切 + OCR 识别文本，组装 Message 列表。
"""

# deque：行去重缓冲（本模块当前用列表实现，保留导入待用）
from collections import deque

# os：环境变量读取（拼图调试展示开关 QQ_SHOW_COMPOSE）
import os

# cv2：轮廓检测（findContours/boundingRect）
import cv2
# numpy：像素数组与颜色掩码运算
import numpy as np
# PIL.Image / ImageDraw：图像裁切与调试绘制
from PIL import Image, ImageDraw

# Message 数据模型（链式 setter）
from .models import Message
# 消息区区域识别
from .regions import get_message_box_region_and_image
# 窗口就绪保证 + 主窗口/截图
from .window_ops import get_main_window, get_qq_window_image, ensure_qq_window_with_retry
# OCR 引擎单例
from ..vision.ocr import OCREngine
# 拼图 OCR：气泡拼接一次识别 + 按 y 边界切分文本（性能优化）
from ..vision.compose import compose_bubbles_to_one, split_ocr_by_bubble
# DPI 查询（头像边距自适应）
from ..core.screenshot import getDPI
# 计时装饰器
from ..core.timing import timer
# 用户列表（read_messages 内部激活目标会话）
from .user_list import UserList


def _show_composed_image(composed: Image.Image) -> None:
    """保存拼图到项目 debug/ 目录并调用系统查看器打开（运行时调试展示）。

    受环境变量 QQ_SHOW_COMPOSE=1 控制：置位时每次读取消息都会弹窗显示
    拼装好的气泡大图；默认关闭（生产零开销、不弹窗）。
    """
    # Path：拼接 debug 目录；time：文件名时间戳（避免覆盖）
    from pathlib import Path
    import time as _time
    # debug 目录 = 项目根/debug（已被 .gitignore 排除，不进入版本库）
    debug_dir = Path(__file__).parent.parent.parent / "debug"
    # 目录不存在则创建
    debug_dir.mkdir(exist_ok=True)
    # 文件名带时间戳：每次刷新独立文件，不互相覆盖
    path = debug_dir / f"qq_compose_{int(_time.time())}.png"
    # 保存拼图
    composed.save(path)
    # 打印保存路径（控制台可追踪）
    print(f"[DEBUG] 拼图已保存: {path}")
    # 调用系统默认图片查看器打开（用户实时查看拼图效果）
    composed.show()


class MessageList:
    """QQ 消息框管理器（单例）。

    截取消息区域 → 按背景色反推气泡坐标 → 裁切 → 逐条 OCR / 判断发送方。
    """

    # 类级单例实例
    _instance: "MessageList | None" = None

    def __new__(cls):
        # 单例：只创建一次
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        # 已初始化过则跳过
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        # 消息列表（最新刷新结果）
        self.messages: list[Message] = []
        # 消息区缓存图（已裁掉头像带）
        self.message_image: Image.Image | None = None
        # 消息区屏幕坐标（点击用）
        self.message_screen_region: dict | None = None
        # 消息区截图内坐标
        self.message_image_region: dict | None = None  # {x, y, w, h, left, top, right, bottom}
        # 关联的 QQ 主窗口句柄
        self.hwnd: int = 0

    # ── 工具 ──────────────────────────────────────────────────
    @staticmethod
    def crop_avatar_area(img: Image.Image) -> Image.Image:
        """裁掉左右头像区域（DPI 自适应）。"""
        # 边距 = 60px × DPI 缩放（高 DPI 屏头像更大，边距相应放大）
        margin = int(60 * getDPI())
        w = img.width
        # 只保留中间"消息文本带"（左右各裁掉头像宽度）
        return img.crop((margin, 0, w - margin, img.height))

    # ── 气泡检测 ────────────────────────────────────────────────
    #
    # 整体思路（三步）：
    #   ① preProcess      把三种难以区分的浅色换成对比鲜明的颜色。
    #   ② detect_bubbles  从换色后的图里，按"黑=对方 / 红=我方"提取气泡外包围盒。
    #   ③ refresh_messageList 把坐标画到原图上，便于肉眼验证检测是否准确。
    #
    # 为什么必须先换色？
    #   QQ 聊天区里三种颜色过于接近，直接用容差很难稳定区分：
    #      背景   (245,245,245)  中浅灰
    #      对方泡 (255,255,255)  纯白  ← 与背景只差 10，最难区分
    #      我方泡 (204,235,255)  浅蓝  ← 与背景偏蓝，相对好区分
    #   若直接在原始颜色上抓连通块，白色气泡极易与背景糊在一起。
    #   所以先把它们换成"黑白红"这种天差地别的颜色，再检测就非常干净。

    # ── 固定参数（可根据实际截图微调） ────────────────────────
    BG_RGB = np.array([245, 245, 245])  # 聊天区背景色（仅作参考/调试）
    MIN_AREA = 550  # 气泡最小面积（px），过滤细碎噪点
    MIN_W = 36  # 气泡最小宽度（px）
    MIN_H = 30  # 气泡最小高度（px）

    @staticmethod
    def preProcess(img_rgb: np.ndarray, bg_tolerance: int = 13) -> np.ndarray:
        """像素级颜色替换，把难区分的浅色拉成黑白红三色（仍返回 RGB 图）。

        做的事情：
          1. 新建一张与输入同尺寸的图，初始全部填 255（纯白）。
             → 这句的作用是"凡是不属于两类气泡的像素，统一变白"，
               从而把背景 245、头像残边、其他杂色等统统剔除成纯白。
          2. 找出"精确等于 255 "的像素（= 对方白色气泡底色），置为 0（纯黑）。
             → 对方气泡从白色 → 黑色，与背景拉开天地之差。
          3. 找出"精确等于 (204,235,255)"的像素（= 我方浅蓝气泡底色），
             置为 (255,0,0)（纯红）。
             → 我方气泡从浅蓝 → 红色。
          4. 气泡内部原本的深色文字（如接近黑的字）因为它不等于 255、
             也不等于 (204,235,255)，会被第 1 步留下的白(255)覆盖。
             → 所以气泡底色(黑/红)里会呈现出"白色文字"，文字被自动分离。

        返回：RGB 图。像素取值只有三种结果：
              (0,0,0)          → 对方气泡
              (255,0,0)        → 我方气泡
              (255,255,255)    → 背景 / 被剔除区

        :param img_rgb: RGB 三通道数组（注意是 RGB，不是 BGR）。
        :param bg_tolerance: 参数已保留但当前精确匹配用不到，仅为接口兼容。
        """
        # 创建全白基底：默认"什么都不要"，只有下面明确命中的才保留。
        out = np.full(img_rgb.shape, 255, dtype=img_rgb.dtype)

        # keep_black：逐像素判断该像素三通道是否都 == 255（纯白）。
        # np.all(..., axis=2) 在最后一个轴（颜色通道）上做"与"，
        # 得到一张与图同尺寸的 bool 掩码，True 的位置就是纯白像素。
        keep_black = np.all(img_rgb == 255, axis=2)  # 白色气泡 → 要保留为黑

        # keep_red：三通道分别 == (204, 235, 255) 的像素 → 要保留为红。
        keep_red = np.all(img_rgb == [204, 235, 255], axis=2)  # 浅蓝气泡 → 红

        # 把命中的位置涂成对应颜色（其余保持第 1 步的全白）。
        out[keep_black] = [0, 0, 0]
        out[keep_red] = [255, 0, 0]

        return out

    @staticmethod
    def detect_bubbles(
            img: Image.Image,
    ) -> list[tuple[int, int, int, int, bool]]:
        """据预处理结果提取每个气泡的外包围盒坐标。

        处理流程：
          1. 把 PIL.Image 转成 numpy 数组（RGB）。
          2. 调用 preProcess，得到"黑/红/白"三色图。
          3. 拆出两张二值掩码：
               - black_mask：黑(0)像素 = 对方气泡所在的区域
               - red_mask  ：红(255,0,0)像素 = 我方气泡所在的区域
          4. 对每张掩码用 cv2.findContours(RETR_EXTERNAL) 找"最外层轮廓"。
             RETR_EXTERNAL 只取外轮廓，因此气泡内部的白字空洞会被自动忽略，
             得到的就是每个气泡的完整外边框。
          5. 对每个轮廓 cv2.boundingRect 求外接矩形 = (x, y, w, h)。
          6. 用 MIN_W / MIN_H / MIN_AREA 过滤掉过小的噪点块。
          7. 把黑/红两类坐标合并，按"从上到下、同行为从左到右"排序。

        返回：[(x, y, w, h, is_self), ...]
              x,y = 气泡左上角（相对裁剪后消息区的坐标）
              w,h = 气泡宽高
              is_self = True  → 我方（红泡）；False → 对方（黑泡）
        """
        # 1. PIL → numpy RGB 数组。
        img_rgb = np.array(img)

        # 2. 颜色替换预处理（白→黑 / 蓝→红 / 其余→白）。
        proc = MessageList.preProcess(img_rgb)

        # 3. 拆分两类气泡的掩码。
        #    黑(0,0,0) → 对方气泡；红(255,0,0) → 我方气泡。
        #    注意：proc 里背景是白(255)，不会是黑/红，所以天然不会误混进来。
        black_mask = np.all(proc == 0, axis=2)
        red_mask = (
                (proc[:, :, 0] == 255)  # R == 255
                & (proc[:, :, 1] == 0)  # G == 0
                & (proc[:, :, 2] == 0)  # B == 0
        )

        boxes: list[tuple[int, int, int, int, bool]] = []

        # 4. 分别处理黑(对方)和红(我方)两类掩码。
        for mask, is_self in ((black_mask, False), (red_mask, True)):
            # OpenCV 的 findContours 需要 uint8 单通道，bool 需先转 uint8。
            m = mask.astype(np.uint8)
            # 轮廓查找
            # RETR_EXTERNAL=只取最外层轮廓（忽略内部空洞，即白字）。
            # CHAIN_APPROX_SIMPLE=压缩轮廓点，减少内存、加快后续计算。
            contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            for c in contours:
                # 每个轮廓的最小外接轴对齐矩形。
                x, y, w, h = cv2.boundingRect(c)

                # 过滤噪点：太窄、太矮、面积太小的都当噪声丢弃。
                if w < MessageList.MIN_W or h < MessageList.MIN_H:
                    continue
                if w * h < MessageList.MIN_AREA:
                    continue

                # 记录：坐标 + 是否我方。
                boxes.append((x, y, w, h, is_self))

        # 7. 按 top(y) 升序排序后，做"按行去重"（每行仅保留最外层气泡）。
        boxes.sort(key=lambda b: (b[1], b[0]))
        boxes = MessageList.dedup_rows(boxes)

        # 8. 用"左右位置"最终判定发送方（is_self），覆盖颜色带来的歧义。
        #    为什么更可靠？
        #      颜色替换有歧义：白色既可能是对方气泡底色，也可能是我方气泡
        #      内部的失败占位，单靠颜色会误判。而位置很稳定：
        #      我方消息气泡永远靠右，对方永远靠左。
        #    判定规则：气泡中心点 x 在图片右半区 → 我方(ME)；左半区 → 对方(THEM)。
        #    （注意：气泡中心以 x+w/2 计，图片中轴以 width/2 计。）
        img_w = img.width
        for i, (x, y, w, h, _old_self) in enumerate(boxes):
            center_x = x + w / 2.0
            is_right = center_x > img_w / 2.0  # 靠右=我方
            boxes[i] = (x, y, w, h, is_right)
        return boxes

    @staticmethod
    def overlap_y(a: tuple, b: tuple) -> bool:
        """判断两个气泡框在竖直方向是否有重叠（视为"同一行"）。

        QQ 消息列表每行至多一个气泡，因此竖直上重叠的多框通常是：
        - 真气泡 + 被包含的误检小块（滚动截半的占位 / 内部杂色）
        归并时应把这类重叠项合并，只留下真正的（最外层）气泡。

        :param a: (x, y, w, h, is_self)
        :param b: (x, y, w, h, is_self)
        """
        # 两个框各自的竖直区间 [top, bottom]
        ay1, ay2 = a[1], a[1] + a[3]
        by1, by2 = b[1], b[1] + b[3]
        # 两框在竖直区间有交集 ⇒ 同一行
        return ay1 < by2 and by1 < ay2

    @staticmethod
    def dedup_rows(
            boxes: list[tuple[int, int, int, int, bool]],
    ) -> list[tuple[int, int, int, int, bool]]:
        """按"每行只保留一个最外层气泡"去重。

        问题：当图片占位在滚动时被截一半，它没被我方气泡完全包围，
        会被误检成一个独立的"对方气泡"小黑块。此时同一行出现两个框：
             ① 我方大红色气泡（真正消息）
             ② 被截半的黑色占位（误检）
        依据 QQ 每行仅一条消息的属性，把它们并到同一行，并只保留
        "面积/跨度最大（最外层）"的那个，丢弃被包含的误检小块。

        实现：按 top 顺序遍历，把竖直重叠的框归为一组，每组取最大框。
        """
        # 按 top 升序排列
        ordered = sorted(boxes, key=lambda b: b[1])  # 按 top 升序
        merged: list[tuple[int, int, int, int, bool]] = []
        for b in ordered:
            # 若 b 与已合并的最后一个框同处一行（竖直重叠），则归并取最大
            if merged and MessageList.overlap_y(merged[-1], b):
                # 取面积更大的作为该行唯一气泡（最外层往往更大）
                if b[2] * b[3] > merged[-1][2] * merged[-1][3]:
                    merged[-1] = b
            else:
                # 新的一行 → 直接加入
                merged.append(b)
        # 重新按 从上到下、同行为从左到右 排序
        merged.sort(key=lambda b: (b[1], b[0]))
        return merged

    def crop_bubble(self, box: tuple[int, int, int, int], pad: int = 6) -> Image.Image:
        """根据单个气泡坐标从缓存消息区裁切，四边留白防裁到边界。

        注意：必须按"气泡自身的 x/y 宽高"裁切，而不是整张图宽。
        之前版本误用 (0, y1, 图宽, y2)，导致裁剪宽度恒等于整个消息区，
        远大于气泡实际宽度。

        :param box: 气泡坐标 (x, y, w, h)
        :param pad: 四周留白像素
        :return: 裁切好的气泡 PIL.Image
        """
        # 解包气泡坐标
        x, y, w, h = box
        # 消息区图尺寸（裁切边界钳制用）
        img_w = self.message_image.width
        img_h = self.message_image.height
        # 四边外扩 pad 像素（不越界）
        x1 = max(0, x - pad)
        y1 = max(0, y - pad)
        x2 = min(img_w, x + w + pad)
        y2 = min(img_h, y + h + pad)
        # 裁出气泡图
        return self.message_image.crop((x1, y1, x2, y2))

    # ── 刷新 ────────────────────────────────────────────────────
    def refresh_image(self):
        """截取消息区域并缓存（窗口不就绪时抛 QQWindowNotReadyError）。"""
        # 确保窗口就绪并取主窗口
        main_window = ensure_qq_window_with_retry()
        # 识别消息区（含截图与坐标）
        result = get_message_box_region_and_image(main_window)
        # 缓存屏幕坐标 / 截图内坐标
        self.message_screen_region = result.screen_region
        self.message_image_region = result.image_region
        # 缓存消息区图（裁掉左右头像带）
        self.message_image = MessageList.crop_avatar_area(result.image)
        # 记录窗口句柄
        self.hwnd = main_window._hWnd

    @timer
    def refresh_messageList(self):
        """解析消息区域：拼图一次 OCR 识别全部气泡，构造 Message 对象存入 self.messages。

        性能优化：所有气泡垂直拼成一张图 → 一次 OCR → 按 y 边界切分文本，
        取代"每气泡单独 OCR"（N 次模型推理 → 1 次推理，显著降低总耗时）。
        """
        # 必须先 refresh_image() 拿到缓存图，否则没有数据可检测
        if self.message_image is None:
            print("[WARN] 请先调用 refresh_image()")
            return

        # 检测所有气泡（含我方/对方）。
        boxes = self.detect_bubbles(self.message_image)

        # 每次刷新重建消息列表（避免上次结果残留）。
        new_messages: list[Message] = []
        # 无气泡（空会话/无消息）→ 直接置空返回
        if not boxes:
            self.messages = new_messages
            return

        # 收集全部气泡小图（按检测顺序，与 boxes 一一对应）。
        bubble_images = [self.crop_bubble((x, y, w, h)) for (x, y, w, h, _) in boxes]
        # 拼图：统一宽度垂直拼接，返回 (大图, 各气泡 y 区间)。
        composed, bounds = compose_bubbles_to_one(bubble_images)

        # 运行时调试展示：QQ_SHOW_COMPOSE=1 时保存拼图并弹窗给用户查看（默认关闭零开销）
        if os.environ.get("QQ_SHOW_COMPOSE") == "1":
            _show_composed_image(composed)

        # 一次 OCR 整张拼图（单例引擎复用，避免重复加载模型）。
        ocr_engine = OCREngine()
        lines = ocr_engine.ocr(np.array(composed)) or []
        # 按 y 边界把识别行切分回各气泡，得到每个气泡的文本（与 boxes 顺序一致）。
        texts = split_ocr_by_bubble(lines, bounds)

        # 遍历每个气泡：组装 Message（缓存坐标、发送方与识别文字）。
        for (x, y, w, h, is_self), text in zip(boxes, texts, strict=True):
            # 用链式 setter 构造 Message 对象
            msg = Message().setRect((x, y, w, h)).setIsSelf(is_self).setText(text)
            new_messages.append(msg)

        # 整体替换消息列表。
        self.messages = new_messages

    @timer
    def refresh(self):
        """一次性刷新消息区并重建消息列表（内部自动 refresh_image + refresh_messageList）。"""
        # 截取消息区
        self.refresh_image()
        # 检测气泡 + OCR
        self.refresh_messageList()

    def read_messages(self, contact_name: str) -> list[Message]:
        """供智能体读取消息的唯一入口：内部激活目标会话后自动刷新，返回最新消息列表。

        读取前在方法内部自动激活该用户（点击列表行切换会话，已激活则跳过），
        调用方无需（也不应）先单独激活——读取本身自带激活语义。
        """
        # 内部激活用户：根据名字点击列表行切换到目标会话（已激活则跳过点击）
        UserList().active_user_by_name(contact_name)
        try:
            # 全量刷新（截图 + 气泡检测 + OCR）
            self.refresh()
            return self.messages
        except RuntimeError as exc:
            # 只重试窗口/区域定位可恢复错误，避免把 OCR 或编程错误也重复执行。
            print(f"[WARN] 消息区刷新失败，重新激活后重试一次: {exc}")
            UserList().active_user_by_name(contact_name)
            self.refresh()
            return self.messages
