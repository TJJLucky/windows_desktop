"""Windows 桌面 GUI 自动化原语，供组合工具使用。

三层子包结构（无循环引用）：
- ``core/`` — 平台原子原语（截图、鼠标、窗口），零 utils 内部依赖
- ``vision/`` — 图像分析工具（模板匹配、OCR），仅依赖 core
- ``qq/`` — QQ 业务组合（激活、区域识别、联系人），依赖 core + vision

顶层重导出保持向后兼容：``utils.screenshot`` → ``utils.core.screenshot`` 等。
"""

# 重导出 core 层四个原子模块：截图 / 鼠标 / 窗口 / 计时
from .core import screenshot
from .core import mouse
from .core import window
from .core import timing
# 重导出 vision 层模板匹配模块（供业务组合直接使用）
from .vision import matcher
# 重导出 qq 层三个业务模块：窗口激活 / 区域识别 / 联系人列表 / 单消费者队列
from .qq import window_ops
from .qq import regions
from .qq import user_list
from .qq import dispatcher

# 包加载时立即固定 DPI-aware（per-monitor V2）：
# 之后 GetWindowRect / WGC / SetCursorPos 全部按物理像素工作，
# 坐标换算统一为"不再除以缩放系数"，避免缩放显示器上的定位错位
screenshot.ensure_dpi_aware()  # 启动时固定 DPI-aware，统一物理像素坐标

# 声明包级公开 API：外部 `import utils` 后通过 utils.<模块名> 访问上述能力
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
