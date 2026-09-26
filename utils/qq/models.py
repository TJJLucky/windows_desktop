"""QQ 业务数据模型：联系人（User）与单条消息（Message）。

纯数据类 + 链式 setter，不依赖任何其他子包，保证序列化结构稳定：
- User.to_dict()  → {"name", "avatar": [cx, cy, r], "rect": [l, t, r, b], "active", "new_msg"}
- Message         → text / rect / is_self 三字段
"""

# dataclass：自动生成 __init__/__repr__ 等样板代码的数据类
from dataclasses import dataclass


@dataclass
class User:
    """QQ 好友数据模型，含截图坐标、屏幕坐标换算与点击方法。"""

    # 文字信息
    name: str = ""  # 好友昵称/备注

    # 列表内行区域坐标 (left, top, right, bottom) rect为截图内坐标
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)

    # 头像圆心 + 半径 (cx, cy, r)
    avatar: tuple[int, int, int] = (0, 0, 0)

    # 是否为当前激活（点开的）会话
    active: bool = False
    # 是否有未读消息红点
    new_msg: bool = False  # 是否有未读消息红点

    def setAvatar(self, avatar: tuple[int, int, int]) -> "User":
        """设置头像圆心 + 半径，返回 self 支持链式调用。"""
        self.avatar = avatar
        return self

    def setRect(self, rect: tuple[int, int, int, int]) -> "User":
        """设置列表内行区域坐标，返回 self 支持链式调用。"""
        self.rect = rect
        return self

    def setActive(self, active: bool):
        """设置是否激活会话，返回 self 支持链式调用。"""
        self.active = active
        return self

    def setName(self, name: str) -> "User":
        """设置昵称，返回 self 支持链式调用。"""
        self.name = name
        return self

    def setNewMsg(self, new_msg: bool) -> "User":
        """设置未读红点标记，返回 self 支持链式调用。"""
        self.new_msg = new_msg
        return self

    def to_dict(self) -> dict:
        """返回 User 的可序列化完整信息（name/avatar/rect/active/new_msg）。"""
        return {
            "name": self.name,
            # 头像：圆心坐标 + 半径（元组转列表便于 JSON 序列化）
            "avatar": list(self.avatar),  # (cx, cy, r)
            # 行区域：四边坐标（元组转列表）
            "rect": list(self.rect),  # (left, top, right, bottom)
            "active": self.active,
            "new_msg": self.new_msg,
        }


@dataclass
class Message:
    """单条消息数据模型，含 OCR 文本、截图像素坐标与发送方判断。

    参考 User 的链式 setter 风格：每个字段有对应 setXxx() 方法，
    返回 self 以便链式构造，例如:
        Message().setText("你好").setRect((0, 0, 10, 10)).setIsSelf(True)
    """

    text: str = ""  # OCR 识别文本
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # 气泡在消息区内的坐标 (x, y, w, h)
    is_self: bool = False  # True=自己发的, False=对方

    def setText(self, text: str) -> "Message":
        """设置消息文本，返回 self 支持链式调用。"""
        self.text = text
        return self

    def setRect(self, rect: tuple[int, int, int, int]) -> "Message":
        """设置气泡在消息区内的坐标，返回 self 支持链式调用。"""
        self.rect = rect
        return self

    def setIsSelf(self, is_self: bool) -> "Message":
        """设置是否为自己发送，返回 self 支持链式调用。"""
        self.is_self = is_self
        return self
