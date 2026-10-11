"""分屏识别与重新定位回归；不操作真实 QQ。"""

import time
from types import SimpleNamespace

from PIL import Image
import pytest

from utils.qq import contact_scan
from utils.qq.contact_scan import ContactNotFoundError, ContactScanner
from utils.qq.models import User
from utils.qq.regions import RegionResult
from utils.qq.user_list import UserList


@pytest.fixture
def paged_list(monkeypatch):
    monkeypatch.setattr(UserList, "_instance", None)
    users = UserList()
    users.hwnd = 123
    users.userList_region = RegionResult(
        screen_region=dict(left=100, top=200, right=400, bottom=800),
        image_region=dict(left=0, top=100, right=300, bottom=700),
        image=Image.new("RGB", (300, 600)),
        full_image=Image.new("RGB", (900, 800)),
    )
    users.last_active_name = "A"
    state = SimpleNamespace(page=0, scrolls=[], refreshes=0,
                            pages=[("A", "B"), ("B", "C"), ("C", "D")])

    def refresh():
        state.refreshes += 1
        users.users = {name: User(name=name, avatar=(25, 50 + index * 60, 20),
                                 rect=(0, 30 + index * 60, 300, 70 + index * 60))
                       for index, name in enumerate(state.pages[state.page])}
        users.cache_data = users.to_dict()
        users.cache_ts = time.time()

    def scroll(scanner, delta):
        state.scrolls.append(delta)
        state.page = max(0, state.page - 1) if delta > 0 else min(state.page + 1, len(state.pages) - 1)
        users.clear_user_list_cache()

    monkeypatch.setattr(users, "refresh", refresh)
    monkeypatch.setattr(ContactScanner, "_scroll_list", scroll)
    refresh()
    return users, state


def test_visible_query_reuses_cache_and_never_scrolls(paged_list):
    users, state = paged_list
    previous = state.refreshes
    assert set(users.get_user_list()) == {"A", "B"}
    assert state.refreshes == previous
    assert state.scrolls == []
    assert set(users.get_user_list("A")) == {"A", "B"}
    assert state.scrolls == []


def test_named_query_falls_back_to_scrolling_and_retains_only_current_coordinates(paged_list):
    users, state = paged_list
    result = users.get_user_list("D")
    assert set(result) == {"C", "D"}
    assert state.page == 2
    assert state.scrolls == [120, -120, -120]
    assert set(users.users) == {"C", "D"}
    assert result["D"]["avatar"] == [25, 110, 20]


def test_current_conversation_missing_from_view_triggers_fallback(paged_list):
    users, state = paged_list
    state.page = 2
    users.refresh()
    result = users.get_user_list()
    assert "A" in result
    assert state.scrolls == [120, 120]


def test_activation_locates_offscreen_target_with_current_page_coordinates(paged_list, monkeypatch):
    users, state = paged_list
    users.get_user_list()
    clicked = []
    monkeypatch.setattr(users, "active_user", lambda user: clicked.append((state.page, user.avatar)) or True)
    monkeypatch.setattr(users, "_wait_panel_switched", lambda name: name)
    assert users.active_user_by_name("D") == "D"
    assert clicked == [(2, (25, 110, 20))]
    assert users.cache_ts == 0


def test_missing_target_stops_at_bottom_without_clicking(paged_list, monkeypatch):
    users, state = paged_list
    monkeypatch.setattr(users, "active_user", lambda user: pytest.fail("must not click"))
    with pytest.raises(ContactNotFoundError):
        users.active_user_by_name("Missing")
    assert state.page == 2
    assert set(users.users) == {"C", "D"}
    assert users.cache_ts == 0


@pytest.mark.parametrize("change", ["hwnd", "geometry", "conversation"])
def test_context_changes_abort_without_scrolling_changed_window(paged_list, monkeypatch, change):
    users, state = paged_list
    original_refresh = users.refresh

    def refresh():
        original_refresh()
        if state.page == 1:
            if change == "hwnd":
                users.hwnd += 1
            elif change == "geometry":
                users.userList_region.screen_region["left"] += 10
            else:
                users.last_active_name = "Other"

    monkeypatch.setattr(users, "refresh", refresh)
    with pytest.raises(RuntimeError, match="QQ_CONTACT_SCAN_CHANGED"):
        users.get_user_list("D")
    assert state.scrolls == [120, -120]
    assert users.cache_ts == 0


def test_page_limit_never_returns_partial_success(paged_list, monkeypatch):
    users, state = paged_list
    monkeypatch.setattr(contact_scan, "CONTACT_SCAN_MAX_PAGES", 2)
    with pytest.raises(RuntimeError, match="QQ_CONTACT_SCAN_LIMIT"):
        users.get_user_list("D")
    assert state.scrolls == [120]
    assert state.page == 0


def test_timeout_stops_scan_without_further_mouse_operations(paged_list, monkeypatch):
    users, state = paged_list
    monkeypatch.setattr(contact_scan, "CONTACT_SCAN_TIMEOUT_SECONDS", -1)
    with pytest.raises(RuntimeError, match="QQ_CONTACT_SCAN_LIMIT"):
        users.get_user_list("D")
    assert state.scrolls == []


def test_failed_scan_does_not_refresh_or_scroll_after_limit(paged_list, monkeypatch):
    users, state = paged_list
    monkeypatch.setattr(contact_scan, "CONTACT_SCAN_MAX_PAGES", 1)
    before = state.refreshes
    with pytest.raises(RuntimeError, match="QQ_CONTACT_SCAN_LIMIT"):
        users.get_user_list("D")
    assert users.cache_ts == 0
    assert state.refreshes == before + 1  # named query 的初始刷新
    assert state.scrolls == []


def test_target_above_current_view_is_checked_before_turning_down(paged_list):
    users, state = paged_list
    state.page = 2
    users.refresh()
    assert "B" in users.get_user_list("B")
    assert state.scrolls == [120]
    assert state.page == 1


@pytest.mark.parametrize("pixels_per_notch, expected_delta", [(16, -720), (160, -360)])
def test_scroll_step_uses_real_displacement_and_keeps_two_rows_overlap(paged_list, monkeypatch, pixels_per_notch, expected_delta):
    users, state = paged_list
    state.offset = 0

    def refresh():
        users.users = {name: User(name=name, avatar=(25, 50 + index * 60 - state.offset, 20))
                       for index, name in enumerate("ABCDEF")}
        users.cache_data = users.to_dict()

    def scroll(scanner, delta):
        state.scrolls.append(delta)
        state.offset = max(0, state.offset - delta / 120 * pixels_per_notch)

    monkeypatch.setattr(users, "refresh", refresh)
    monkeypatch.setattr(ContactScanner, "_scroll_list", scroll)
    monkeypatch.setattr(contact_scan, "CONTACT_SCAN_MAX_PAGES", 4)
    with pytest.raises(RuntimeError, match="QQ_CONTACT_SCAN_LIMIT"):
        users.get_user_list("Missing")
    assert state.scrolls == [120, -120, expected_delta]
    assert abs(expected_delta) / 120 * pixels_per_notch <= 600 - 2 * 60


def test_bottom_detection_tolerates_partial_ocr_variation(paged_list, monkeypatch):
    users, state = paged_list
    snapshots = [("A", "B", "C", "D"), ("A", "B", "C", "D?"),
                 ("E", "F", "G", "H"), ("E", "F", "G", "H?")]
    def refresh():
        names = snapshots[min(len(state.scrolls), len(snapshots) - 1)]
        users.users = {name: User(name=name, avatar=(25, 50 + index * 60, 20))
                       for index, name in enumerate(names)}
        users.cache_data = users.to_dict()
    monkeypatch.setattr(users, "refresh", refresh)
    with pytest.raises(ContactNotFoundError):
        users.get_user_list("Missing")
    assert state.scrolls == [120, -120, -120]


def test_empty_ocr_does_not_claim_bottom_or_contact_absence(paged_list, monkeypatch):
    users, state = paged_list
    state.pages = [()]
    monkeypatch.setattr(contact_scan, "CONTACT_SCAN_MAX_PAGES", 3)
    with pytest.raises(RuntimeError, match="QQ_CONTACT_SCAN_LIMIT"):
        users.get_user_list("Missing")


def test_ambiguous_partial_names_do_not_select_first(paged_list):
    users, _ = paged_list
    users.users = {name: User(name=name) for name in ("SupplierEast", "SupplierWest")}
    with pytest.raises(RuntimeError, match="QQ_CONTACT_AMBIGUOUS"):
        users.find_user("Supplier")
    assert users.find_user("SupplierEast").name == "SupplierEast"


def test_wheel_uses_list_center_and_two_second_wait(paged_list, monkeypatch):
    users, _ = paged_list
    calls = []
    class Capture:
        def __init__(self, hwnd):
            assert hwnd == 123
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(contact_scan, "WindowCaptureCtx", Capture)
    monkeypatch.setattr(contact_scan, "scroll_at", lambda *args: calls.append(args))
    monkeypatch.setattr(contact_scan.time, "sleep", lambda seconds: calls.append(seconds))
    # fixture 替换了类方法，这里直接调用原始实现。
    _REAL_SCROLL(ContactScanner(users), -120)
    assert calls == [(250, 500, -120), 2.0]


_REAL_SCROLL = ContactScanner._scroll_list
