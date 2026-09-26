"""图像分析工具：模板匹配、OCR、颜色提取。仅依赖 core 子包。

本层位于依赖树中层：向上给 qq 业务层提供"看"的能力，向下只依赖 core（截图/窗口）。
"""

# 导入三个图像分析模块：模板匹配 / OCR / 视觉编排占位
from . import matcher
from . import ocr
from . import compose

# 包级公开 API：utils.vision.matcher / ocr / compose
__all__ = [
    "matcher",
    "ocr",
    "compose",
]
