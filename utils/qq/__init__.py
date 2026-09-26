"""QQ 业务组合层：QQ 窗口激活、区域识别、联系人列表等。依赖 core + vision。"""

from . import window_ops
from . import regions
from . import user_list
from . import input
from . import dispatcher
from . import models

__all__ = [
    "window_ops",
    "regions",
    "user_list",
    "input",
    "dispatcher",
    "models",
]

