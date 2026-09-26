"""QQ 业务组合层：QQ 窗口激活、区域识别、联系人列表等。依赖 core + vision。

本层是业务出口：把 core（截图/鼠标/窗口）与 vision（模板匹配/OCR）组合成
"QQ 桌面自动化"的高层能力；service 层的 Dispatcher 门面再从这里入队调用。
"""

# 导入六个业务模块：窗口激活 / 区域识别 / 联系人列表 / 输入框 / 单消费者队列 / 数据模型
from . import window_ops
from . import regions
from . import user_list
from . import input
from . import dispatcher
from . import models

# 包级公开 API：utils.qq.<模块名>
__all__ = [
    "window_ops",
    "regions",
    "user_list",
    "input",
    "dispatcher",
    "models",
]
