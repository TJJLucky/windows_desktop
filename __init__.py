"""Windows 桌面 GUI 自动化包。

分层结构：
- ``utils/`` — 原子原语（窗口、截图、OCR、鼠标键盘），按 core/vision/qq 分层

非 Windows 平台会 import 成功但实际调用会失败。
"""

from . import utils

__all__ = [
    "utils",
]
