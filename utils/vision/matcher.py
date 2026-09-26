"""图像匹配原语：模板匹配、裁剪。仅依赖 core 子包。

QQ 专用匹配函数（match_switch_to_*）已迁至 qq/window_ops.py。

核心能力：
- TemplateMatcher：带 alpha 透明掩码、候选 NMS 去重、多尺度匹配的模板匹配器；
- find_template：兼容旧调用的单结果封装；
- 裁剪工具：按矩形/字典区域裁图（供 OCR 前取 ROI）。
"""

# cv2：OpenCV 图像处理（灰度化、匹配、膨胀、NMS 所需）
import cv2
# numpy：数组运算（分数矩阵、坐标筛选）
import numpy as np
# PIL.Image：图像输入/输出格式（上层统一用 PIL）
from PIL import Image


class TemplateMatcher:
    """GUI 模板匹配器，支持 alpha mask、候选去重和可选多尺度匹配。"""

    # 默认多尺度集合：应对 DPI/窗口缩放导致的模板与画面尺寸不一致
    DEFAULT_SCALES = (0.85, 0.92, 1.0, 1.08, 1.15)

    def __init__(
            self,
            *,
            nms_iou_threshold: float = 0.3,
            use_clahe: bool = False,
            scales: tuple[float, ...] = DEFAULT_SCALES,
    ) -> None:
        """初始化匹配器。

        :param nms_iou_threshold: 非极大值抑制的 IoU 阈值（>该值视为同一目标去重）
        :param use_clahe: 是否启用 CLAHE 对比度增强（光照不均的截图建议开）
        :param scales: 多尺度候选集合；传 (1.0,) 即固定尺寸
        """
        # 参数校验：IoU 阈值必须是 [0,1]
        if not 0.0 <= nms_iou_threshold <= 1.0:
            raise ValueError("nms_iou_threshold must be between 0 and 1")
        # 参数校验：尺度集合非空且全部为正
        if not scales or any(scale <= 0 for scale in scales):
            raise ValueError("scales must contain positive values")

        # 保存配置
        self.nms_iou_threshold = nms_iou_threshold
        self.use_clahe = use_clahe
        self.scales = tuple(scales)
        # 需要 CLAHE 时预创建对象（clipLimit=2.0 限制对比度放大，8x8 分块）
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)) if use_clahe else None

    def find_candidates(
            self,
            big: Image.Image,
            small: Image.Image,
            threshold: float = 0.8,
            use_mask: bool = False,
            multiscale: bool = True,
    ) -> list[dict]:
        """返回按匹配分数排序、经 NMS 去重后的候选框列表。

        候选中的 ``score`` 始终是越大越匹配的统一分数。透明模板仅在
        ``use_mask=True`` 且 alpha 存在透明像素时使用 ``TM_SQDIFF_NORMED``，
        因为 OpenCV 不支持将 mask 与 ``TM_CCOEFF_NORMED`` 组合使用。
        """
        # 预处理大图（灰度 + 高斯模糊 + 可选 CLAHE）
        big_gray = self._preprocess(np.asarray(big.convert("RGB")))
        # 模板转 RGBA：分离 RGB 与 alpha 通道
        small_rgba = np.asarray(small.convert("RGBA"))
        # 模板灰度图（只取 RGB 三通道）
        template_gray = self._preprocess(small_rgba[:, :, :3])
        # alpha 通道（透明信息）
        alpha = small_rgba[:, :, 3]

        # 判断模板是否存在"半透明像素"（既有 <255 又有 >0）→ 可做 alpha 掩码匹配
        has_transparency = bool(np.any(alpha < 255) and np.any(alpha > 0))
        # 是否启用 alpha 掩码：调用方允许 + 模板确有透明像素
        use_alpha_mask = use_mask and has_transparency
        # 模板全透明（无任何有效像素）→ 没有任何可匹配内容，直接返回空
        if use_mask and not np.any(alpha > 0):
            return []

        # 匹配方法选择：alpha 掩码只能用 TM_SQDIFF_NORMED（分数越小越匹配）；否则 TM_CCOEFF_NORMED（越大越匹配）
        method = cv2.TM_SQDIFF_NORMED if use_alpha_mask else cv2.TM_CCOEFF_NORMED
        method_name = "TM_SQDIFF_NORMED" if use_alpha_mask else "TM_CCOEFF_NORMED"
        # 是否做多尺度：默认全尺度扫描；性能敏感时固定 1.0
        scales = self.scales if multiscale else (1.0,)
        candidates: list[dict] = []

        # 遍历每个尺度
        for scale in scales:
            # 按尺度缩放模板（及 alpha）
            scaled_template, scaled_alpha = self._scale_template(template_gray, alpha, scale)
            # 缩放后的模板尺寸
            template_height, template_width = scaled_template.shape
            # 大图尺寸
            big_height, big_width = big_gray.shape
            # 模板大于大图 → 该尺度不可能匹配，跳过
            if template_width > big_width or template_height > big_height:
                continue

            # 掩码参数（启用时用缩放后的 alpha）
            mask = scaled_alpha if use_alpha_mask else None
            try:
                # 执行模板匹配：返回分数矩阵（每个像素一个分数）
                result = cv2.matchTemplate(big_gray, scaled_template, method, mask=mask)
            except cv2.error:
                # 个别 OpenCV 组合会报错（如掩码尺寸边界），跳过该尺度不致命
                continue

            # 统一分数方向：SQDIFF 的分数越小越匹配 → 用 1-分数 翻转成"越大越匹配"
            scores = 1.0 - result if use_alpha_mask else result
            # 收集局部峰值（>= 阈值且为 3x3 邻域极大值）
            candidates.extend(
                self._collect_local_peaks(
                    scores,
                    threshold,
                    template_width,
                    template_height,
                    scale,
                    method_name,
                )
            )

        # 跨尺度做 NMS 去重（同一目标多个尺度都命中 → 只留分数最高的）
        return self._non_maximum_suppression(candidates)

    def _preprocess(self, rgb: np.ndarray) -> np.ndarray:
        """图像预处理：转灰度 → 高斯模糊降噪 → 可选 CLAHE 增强对比度。"""
        # BGR/RGB 转灰度
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        # 3x3 高斯模糊：平滑噪点，提高匹配稳定性
        blurred = cv2.GaussianBlur(gray, (3, 3), sigmaX=0.8)
        # 启用 CLAHE 时做对比度增强（否则原样返回模糊图）
        return self._clahe.apply(blurred) if self._clahe is not None else blurred

    @staticmethod
    def _scale_template(
            template: np.ndarray,
            alpha: np.ndarray,
            scale: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        """按尺度缩放模板灰度图与 alpha 图，返回 (灰度, alpha)。"""
        # 缩放系数为 1.0 → 原样返回（避免不必要的 resize 开销）
        if scale == 1.0:
            return template, alpha

        # 目标尺寸：宽高 × 尺度，至少 1 像素
        height, width = template.shape
        scaled_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        # 缩小用 INTER_AREA（抗锯齿更好）；放大用 INTER_LINEAR（平滑）
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        return (
            cv2.resize(template, scaled_size, interpolation=interpolation),
            cv2.resize(alpha, scaled_size, interpolation=interpolation),
        )

    @staticmethod
    def _collect_local_peaks(
            scores: np.ndarray,
            threshold: float,
            width: int,
            height: int,
            scale: float,
            method: str,
    ) -> list[dict]:
        """从分数矩阵收集局部峰值（>= 阈值且为 3x3 邻域最大值），转为候选字典列表。"""
        # 防御：非有限值（NaN/Inf）替换为 -Inf，避免比较异常
        valid_scores = np.where(np.isfinite(scores), scores, -np.inf)
        # 局部极大值判定：分数 >= 其 3x3 膨胀结果 → 该点是邻域内最大
        local_maxima = valid_scores >= cv2.dilate(valid_scores, np.ones((3, 3), dtype=np.uint8))
        # 同时满足"超过阈值"和"局部极大"的坐标
        ys, xs = np.where((valid_scores >= threshold) & local_maxima)

        # 组装候选字典（含坐标框、分数、尺度、方法名）
        return [
            {
                "x": int(x),
                "y": int(y),
                "left": int(x),
                "top": int(y),
                "right": int(x + width),
                "bottom": int(y + height),
                "score": float(valid_scores[y, x]),
                "scale": float(scale),
                "method": method,
            }
            for y, x in zip(ys, xs, strict=True)
        ]

    def _non_maximum_suppression(self, candidates: list[dict]) -> list[dict]:
        """NMS 去重：按分数从高到低，仅保留与已选候选 IoU 不超过阈值的新候选。"""
        selected: list[dict] = []
        # 分数从高到低遍历
        for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
            # 与所有已选候选的 IoU 都 ≤ 阈值 → 判定为不同目标，保留
            if all(self._iou(candidate, kept) <= self.nms_iou_threshold for kept in selected):
                selected.append(candidate)
        return selected

    @staticmethod
    def _iou(first: dict, second: dict) -> float:
        """计算两个候选框的交并比（Intersection over Union）。"""
        # 交集矩形：各边取内侧
        left = max(first["left"], second["left"])
        top = max(first["top"], second["top"])
        right = min(first["right"], second["right"])
        bottom = min(first["bottom"], second["bottom"])
        # 交集面积（无边相交则 0）
        intersection = max(0, right - left) * max(0, bottom - top)
        if intersection == 0:
            return 0.0

        # 两框各自面积
        first_area = (first["right"] - first["left"]) * (first["bottom"] - first["top"])
        second_area = (second["right"] - second["left"]) * (second["bottom"] - second["top"])
        # IoU = 交集 / 并集
        return intersection / (first_area + second_area - intersection)


# 模块级默认匹配器（默认参数，供兼容函数复用）
_DEFAULT_TEMPLATE_MATCHER = TemplateMatcher()


def find_template(
        big: Image.Image,
        small: Image.Image,
        threshold=0.8,
        use_mask=False,
        multiscale=True,
):
    """兼容旧调用：返回最佳 {x,y,left,top,right,bottom} 或 None。

    默认启用多尺度匹配以适配 DPI/窗口缩放；对性能敏感的调用可显式传入
    ``multiscale=False`` 恢复固定尺寸匹配。
    """
    # 用默认匹配器找全部候选
    candidates = _DEFAULT_TEMPLATE_MATCHER.find_candidates(
        big, small, threshold, use_mask, multiscale
    )
    # 无候选 → 未找到
    if not candidates:
        return None

    # 取分数最高的候选，只返回位置字段（保持旧调用契约）
    best = candidates[0]
    return {key: best[key] for key in ("x", "y", "left", "top", "right", "bottom")}


def draw_box(image: Image.Image, x: int, y: int, w: int, h: int,
             color=(0, 255, 0), thickness=1) -> Image.Image:
    """在图片上绘制矩形框，返回新 PIL.Image"""
    # PIL RGB → OpenCV BGR 数组
    arr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    # 画矩形（左上角 + 宽高）
    cv2.rectangle(arr, (x, y), (x + w, y + h), color, thickness)
    # 转回 PIL RGB 图返回
    return Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))


def crop_image(image: Image.Image, x: int, y: int, w: int, h: int) -> Image.Image:
    """根据左上角(x,y)和宽高(w,h)裁剪图片"""
    return image.crop((x, y, x + w, y + h))


def crop_region(image: Image.Image, region: dict) -> Image.Image:
    """根据 find_template 返回的 {left,top,right,bottom} 字典裁剪"""
    return image.crop((region["left"], region["top"],
                       region["right"], region["bottom"]))
