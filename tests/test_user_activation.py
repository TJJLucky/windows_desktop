"""用户激活坐标映射单元测试；不操作真实 QQ。"""

from __future__ import annotations

import threading
import time

from PIL import Image
import pytest

from utils.qq.models import User
from utils.qq.regions import RegionResult
from utils.qq import user_list as user_list_module
from utils.qq.user_list import ContactNotFoundError, USER_LIST_CACHE_TTL_SECONDS, UserList


class _DummyCaptureCtx:
    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False



def test_user_list_cache_ttl_is_three_seconds():
    assert USER_LIST_CACHE_TTL_SECONDS == 3.0

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


def test_find_user_supports_punctuation_only_name_and_ignores_empty_ocr_row():
    """纯符号联系人可以匹配，空 OCR 行不能成为通配符。"""
    user_list = UserList()
    symbol_user = User(name="！")
    empty_ocr_user = User(name="")
    user_list.users = {"": empty_ocr_user, "！": symbol_user}

    assert user_list.find_user("！") is symbol_user
    assert user_list.find_user("不存在") is None
    for standalone_name in ("!", "！", "，", "@", "#", "?", "。", "……", "123", "🙂"):
        assert user_list._normalise_name(standalone_name) == standalone_name
    assert user_list._normalise_name("张三！") == "张三"
    assert user_list._name_matches("！", "！") is True



def test_active_user_by_name_forces_user_list_refresh(monkeypatch):
    user_list = UserList()
    calls: list[str] = []
    user = User(name="华强电子", active=True)

    monkeypatch.setattr(
        user_list,
        "_ensure_user_list_fresh",
        lambda force=False: calls.append(f"force={force}"),
    )
    monkeypatch.setattr(user_list, "find_user", lambda name: user)
    monkeypatch.setattr(user_list, "_wait_panel_switched", lambda name: "华强电子")
    monkeypatch.setattr(user_list, "_promote_user_name", lambda old, full: full)

    assert user_list.active_user_by_name("华强电子") == "华强电子"
    assert calls == ["force=True"]


def test_active_user_by_name_stops_after_one_refresh_when_contact_is_missing(monkeypatch):
    """联系人未出现在当前列表时，不重复完整 OCR 刷新。"""
    user_list = UserList()
    calls: list[str] = []

    monkeypatch.setattr(
        user_list,
        "_ensure_user_list_fresh",
        lambda force=False: calls.append(f"force={force}"),
    )
    monkeypatch.setattr(user_list, "find_user", lambda name: None)

    with pytest.raises(ContactNotFoundError, match="未找到联系人「不存在」"):
        user_list.active_user_by_name("不存在")

    assert calls == ["force=True"]


def test_concurrent_force_refreshes_share_single_refresh(monkeypatch):
    user_list = UserList()
    calls: list[float] = []
    barrier = threading.Barrier(6)

    def fake_refresh():
        calls.append(time.time())
        time.sleep(0.05)
        user_list.userList_region = object()
        user_list.users = {"A": User(name="A")}
        user_list.cache_data = {"A": {"name": "A"}}
        user_list.cache_ts = time.time()

    monkeypatch.setattr(user_list, "userList_region", None)
    monkeypatch.setattr(user_list, "cache_ts", 0.0)
    monkeypatch.setattr(user_list, "cache_data", {})
    monkeypatch.setattr(user_list, "refresh", fake_refresh)

    def read():
        barrier.wait()
        user_list._ensure_user_list_fresh(force=True)

    threads = [threading.Thread(target=read) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1


def test_concurrent_refresh_failure_is_shared_and_next_call_retries(monkeypatch):
    user_list = UserList()
    failure = RuntimeError("OCR failed")
    calls = 0
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def failing_refresh():
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        raise failure

    monkeypatch.setattr(user_list, "refresh", failing_refresh)

    def read():
        barrier.wait()
        try:
            user_list._ensure_user_list_fresh(force=True)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == 1
    assert len(errors) == 6
    assert all(error is failure for error in errors)

    recovered = False

    def successful_refresh():
        nonlocal calls, recovered
        calls += 1
        recovered = True

    monkeypatch.setattr(user_list, "refresh", successful_refresh)
    user_list._ensure_user_list_fresh(force=True)

    assert calls == 2
    assert recovered is True
