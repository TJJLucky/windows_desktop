"""用户激活坐标映射单元测试；不操作真实 QQ。"""

from __future__ import annotations

from PIL import Image

from utils.qq.models import User
from utils.qq.regions import RegionResult
from utils.qq import user_list as user_list_module
from utils.qq.user_list import UserList


class _DummyCaptureCtx:
    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def test_active_user_converts_full_image_circle_to_screen_coordinates(monkeypatch):
    user_list = UserList()
    user_list.hwnd = 123
    user_list.userList_region = RegionResult(
        screen_region={"left": 110, "top": 150},
        image_region={"left": 10, "right": 400, "top": 50},
        image=Image.new("RGB", (390, 500)),
        full_image=Image.new("RGB", (500, 700)),
    )
    user = User(
        name="华强电子",
        avatar=(20, 30, 15),
        rect=(0, 15, 390, 45),
    )
    clicks: list[tuple[int, int, int, int]] = []

    monkeypatch.setattr(
        UserList,
        "detect_avatar_circles",
        staticmethod(lambda image: [(350, 80, 15), (430, 80, 15)]),
    )
    monkeypatch.setattr(user_list_module, "WindowCaptureCtx", _DummyCaptureCtx)
    monkeypatch.setattr(user_list_module, "random_click", lambda x, y, w, h: clicks.append((x, y, w, h)))

    assert user_list.active_user(user) is True
    # 窗口原点为 (100, 100)；选中 x=350 的列表圆，并排除 x=430 的右面板圆。
    assert clicks == [(470, 177, 28, 6)]


def test_promote_user_name_replaces_truncated_key(monkeypatch):
    user_list = UserList()
    user = User(name="华强电", avatar=(20, 30, 15), rect=(0, 15, 390, 45))
    monkeypatch.setattr(user_list, "users", {"华强电": user})

    assert user_list._promote_user_name("华强电", "华强电子") == "华强电子"
    assert "华强电" not in user_list.users
    assert user_list.users["华强电子"].name == "华强电子"