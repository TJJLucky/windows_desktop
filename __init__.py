"""Windows 桌面 GUI 自动化包。

分层结构：
- ``utils/`` — 原子原语（窗口、截图、OCR、鼠标键盘），按 core/vision/qq 分层

非 Windows 平台会 import 成功但实际调用会失败。
"""

# 导入子包 utils（包加载时自动触发 DPI-aware 初始化）
from . import utils

# 包级公开 API：本包只公开 utils 子包
__all__ = [
    "utils",
]
