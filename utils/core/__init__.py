"""平台原子原语：截图、鼠标、窗口操作。零依赖其他 utils 子包。

本层是整棵依赖树的根：vision 层（模板匹配/OCR）与 qq 层（业务组合）都只能向下依赖本层。
"""

# 导入四个原子模块：后台截图 / 鼠标模拟 / 窗口操作 / 计时装饰器
from . import screenshot
from . import mouse
from . import window
from . import timing

# 包级公开 API：utils.core.screenshot / mouse / window / timing
__all__ = [
    "screenshot",
    "mouse",
    "window",
    "timing",
]
