"""QQ 业务组合层：QQ 窗口激活、区域识别、联系人列表等。依赖 core + vision。"""

from . import composites
from . import regions
from . import userList
from . import input

__all__ = [
    "composites",
    "regions",
    "userList",
    "input",
]

