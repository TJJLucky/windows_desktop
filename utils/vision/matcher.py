"""图像匹配原语：模板匹配、裁剪。仅依赖 core 子包。

QQ 专用匹配函数（match_overflow_qq_icon、match_switch_to_*）已迁至 qq/window_ops.py。
"""

import cv2
import numpy as np
from PIL import Image


class TemplateMatcher:
    """GUI 模板匹配器，支持 alpha mask、候选去重和可选多尺度匹配。"""

    DEFAULT_SCALES = (0.85, 0.92, 1.0, 1.08, 1.15)

    def __init__(
            self,
            *,
            nms_iou_threshold: float = 0.3,
            use_clahe: bool = False,
            scales: tuple[float, ...] = DEFAULT_SCALES,
    ) -> None:
        if not 0.0 <= nms_iou_threshold <= 1.0:
            raise ValueError("nms_iou_threshold must be between 0 and 1")
        if not scales or any(scale <= 0 for scale in scales):
            raise ValueError("scales must contain positive values")

        self.nms_iou_threshold = nms_iou_threshold
        self.use_clahe = use_clahe
        self.scales = tuple(scales)
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
        big_gray = self._preprocess(np.asarray(big.convert("RGB")))
        small_rgba = np.asarray(small.convert("RGBA"))
        template_gray = self._preprocess(small_rgba[:, :, :3])
        alpha = small_rgba[:, :, 3]

        has_transparency = bool(np.any(alpha < 255) and np.any(alpha > 0))
        use_alpha_mask = use_mask and has_transparency
        if use_mask and not np.any(alpha > 0):
            return []

        method = cv2.TM_SQDIFF_NORMED if use_alpha_mask else cv2.TM_CCOEFF_NORMED
        method_name = "TM_SQDIFF_NORMED" if use_alpha_mask else "TM_CCOEFF_NORMED"
        scales = self.scales if multiscale else (1.0,)
        candidates: list[dict] = []

        for scale in scales:
            scaled_template, scaled_alpha = self._scale_template(template_gray, alpha, scale)
            template_height, template_width = scaled_template.shape
            big_height, big_width = big_gray.shape
            if template_width > big_width or template_height > big_height:
                continue

            mask = scaled_alpha if use_alpha_mask else None
            try:
                result = cv2.matchTemplate(big_gray, scaled_template, method, mask=mask)
            except cv2.error:
                continue

            scores = 1.0 - result if use_alpha_mask else result
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

        return self._non_maximum_suppression(candidates)

    def _preprocess(self, rgb: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        blurred = cv2.GaussianBlur(gray, (3, 3), sigmaX=0.8)
        return self._clahe.apply(blurred) if self._clahe is not None else blurred

    @staticmethod
    def _scale_template(
            template: np.ndarray,
            alpha: np.ndarray,
            scale: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        if scale == 1.0:
            return template, alpha

        height, width = template.shape
        scaled_size = (max(1, round(width * scale)), max(1, round(height * scale)))
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
        valid_scores = np.where(np.isfinite(scores), scores, -np.inf)
        local_maxima = valid_scores >= cv2.dilate(valid_scores, np.ones((3, 3), dtype=np.uint8))
        ys, xs = np.where((valid_scores >= threshold) & local_maxima)

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
        selected: list[dict] = []
        for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
            if all(self._iou(candidate, kept) <= self.nms_iou_threshold for kept in selected):
                selected.append(candidate)
        return selected

    @staticmethod
    def _iou(first: dict, second: dict) -> float:
        left = max(first["left"], second["left"])
        top = max(first["top"], second["top"])
        right = min(first["right"], second["right"])
        bottom = min(first["bottom"], second["bottom"])
        intersection = max(0, right - left) * max(0, bottom - top)
        if intersection == 0:
            return 0.0

        first_area = (first["right"] - first["left"]) * (first["bottom"] - first["top"])
        second_area = (second["right"] - second["left"]) * (second["bottom"] - second["top"])
        return intersection / (first_area + second_area - intersection)


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
    candidates = _DEFAULT_TEMPLATE_MATCHER.find_candidates(
        big, small, threshold, use_mask, multiscale
    )
    if not candidates:
        return None

    best = candidates[0]
    return {key: best[key] for key in ("x", "y", "left", "top", "right", "bottom")}


def draw_box(image: Image.Image, x: int, y: int, w: int, h: int,
             color=(0, 255, 0), thickness=1) -> Image.Image:
    """在图片上绘制矩形框，返回新 PIL.Image"""
    arr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    cv2.rectangle(arr, (x, y), (x + w, y + h), color, thickness)
    return Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))


def crop_image(image: Image.Image, x: int, y: int, w: int, h: int) -> Image.Image:
    """根据左上角(x,y)和宽高(w,h)裁剪图片"""
    return image.crop((x, y, x + w, y + h))


def crop_region(image: Image.Image, region: dict) -> Image.Image:
    """根据 find_template 返回的 {left,top,right,bottom} 字典裁剪"""
    return image.crop((region["left"], region["top"],
                       region["right"], region["bottom"]))
