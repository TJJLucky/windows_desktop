"""Windows 桌面 GUI 自动化原语，供组合工具使用。

三层子包结构（无循环引用）：
- ``core/`` — 平台原子原语（截图、鼠标、窗口），零 utils 内部依赖
- ``vision/`` — 图像分析工具（模板匹配、OCR），仅依赖 core
- ``qq/`` — QQ 业务组合（激活、区域识别、联系人），依赖 core + vision

顶层重导出保持向后兼容：``utils.screenshot`` → ``utils.core.screenshot`` 等。
"""

from .core import screenshot
from .core import mouse
from .core import window
from .core import timing
from .vision import matcher
from .qq import window_ops
from .qq import regions
from .qq import user_list
from .qq import dispatcher

screenshot.ensure_dpi_aware()  # 启动时固定 DPI-aware，统一物理像素坐标

__all__ = [
    "screenshot",
    "mouse",
    "window",
    "timing",
    "matcher",
    "window_ops",
    "regions",
    "user_list",
    "dispatcher",
]
