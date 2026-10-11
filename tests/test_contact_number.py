"""复制号码的独立状态校验，不触碰真实桌面。"""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from PIL import Image

from utils.core import clipboard as cb
from utils.qq import contact_number as cn
from utils.qq.regions import RegionResult
from utils.vision.matcher import find_unique_template, TemplateAmbiguousError


@pytest.fixture
def copy_flow(monkeypatch):
    image = Image.new("RGB", (400, 300), "white")
    rect = dict(left=20, top=60, right=180, bottom=300)
    region = RegionResult(rect, rect, image.crop((20, 60, 180, 300)), image)
    row = cn.ContactRow((30, 35, 15), (0, 10, 160, 60))
    state = SimpleNamespace(clicks=[], closed=[], matches=0, value="800888598", signature=0,
                            missing=False, moved=False, menu_ambiguous=False, after_row=row,
                            clipboard_error=None, changed_header=False, foreground=1)
    signature = dict(hwnd=1, pid=7, left=0, top=0, right=400, bottom=300, dpi=96)
    monkeypatch.setattr(cn, "WindowCaptureCtx", lambda hwnd: nullcontext())
    monkeypatch.setattr(cn, "window_signature", lambda w: {**signature, "dpi": 96 + state.signature})
    monkeypatch.setattr(cn.win32gui, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(cn.win32gui, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(cn.win32gui, "GetForegroundWindow", lambda: state.foreground)
    monkeypatch.setattr(cn, "load_image", lambda key: Image.new("RGB", (10, 10)))
    monkeypatch.setattr(cn, "MENU_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(cn.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cn, "send_hotkey", lambda keys: state.closed.append(keys))
    monkeypatch.setattr(cn, "_active_row", lambda r: state.after_row)
    state.barriers = []
    monkeypatch.setattr(cn.ContactNumberReader, "_clock", staticmethod(lambda: len(state.clicks) * 100))
    def capture(window, *, after_100ns=None):
        state.barriers.append(after_100ns)
        frame = image.copy()
        if state.changed_header:
            frame.paste("black", (180, 0, 400, 60))
        return frame
    monkeypatch.setattr(cn.ContactNumberReader, "_capture", staticmethod(capture))
    def right_click(x, y, **options):
        state.clicks.append(("right", x, y, options))
    monkeypatch.setattr(cn, "right_click_at", right_click)
    monkeypatch.setattr(cn, "click_at", lambda x, y, **opts: state.clicks.append(("left", x, y, opts)))
    def match(image, template, **options):
        assert image.size == region.image.size  # 只在列表范围搜索菜单
        state.matches += 1
        if state.menu_ambiguous:
            raise RuntimeError("QQ_CONTACT_NUMBER_MENU_AMBIGUOUS")
        if state.missing or state.matches >= 3:
            return None
        dx = 10 if state.moved and state.matches == 2 else 0
        return dict(left=25+dx, top=70, right=65+dx, bottom=90)
    monkeypatch.setattr(cn, "find_unique_template", match)
    class Clipboard:
        def mark(self):
            state.clicks.append(("mark",))
        def read(self, **options):
            assert options["expected_pid"] == 7
            if state.clipboard_error:
                raise cb.ClipboardCopyError(state.clipboard_error)
            return state.value
    monkeypatch.setattr(cn, "ClipboardCopy", Clipboard)
    return state, lambda: cn.ContactNumberReader().copy_row(SimpleNamespace(_hWnd=1), region, row)


def test_copy_right_clicks_avatar_center_and_left_clicks_current_menu(copy_flow):
    state, copy = copy_flow
    assert copy() == "800888598"
    assert [c[0] for c in state.clicks] == ["right", "mark", "left"]
    assert state.clicks[0][1:3] == (50, 95)
    assert state.clicks[2][1:3] == (65, 140)
    assert all(c[-1]["smooth"] is False for c in (state.clicks[0], state.clicks[2]))
    assert state.barriers == [None, 100, 100, 300]


@pytest.mark.parametrize("property,value,code", [
    ("moved", True, "MENU_CHANGED"),
    ("menu_ambiguous", True, "MENU_AMBIGUOUS"),
    ("value", "nickname", "INVALID"),
    ("clipboard_error", "CLIPBOARD_UNCHANGED", "CLIPBOARD_UNCHANGED"),
    ("clipboard_error", "CLIPBOARD_OWNER_CHANGED", "CLIPBOARD_OWNER_CHANGED"),
    ("after_row", cn.ContactRow((30, 95, 15), (0, 70, 160, 120)), "ROW_CHANGED"),
    ("changed_header", True, "ROW_CHANGED"),
])
def test_copy_stops_without_replay_on_explicit_failure(copy_flow, property, value, code):
    state, copy = copy_flow
    setattr(state, property, value)
    with pytest.raises(RuntimeError, match=f"QQ_CONTACT_NUMBER_{code}"):
        copy()
    assert sum(c[0] == "right" for c in state.clicks) <= 1
    assert sum(c[0] == "left" for c in state.clicks) <= 1
    if any(c[0] == "right" for c in state.clicks) and state.matches < 3:
        assert state.closed == [("esc",)]


def test_window_change_after_menu_stops_before_left_click(copy_flow, monkeypatch):
    state, copy = copy_flow
    original = cn.right_click_at
    def change(*args, **kwargs):
        original(*args, **kwargs)
        state.signature = 1
    monkeypatch.setattr(cn, "right_click_at", change)
    with pytest.raises(RuntimeError, match="WINDOW_CHANGED"):
        copy()
    assert not any(c[0] == "left" for c in state.clicks)


def test_frame_timeout_after_right_click_does_not_replay_or_click_menu(copy_flow, monkeypatch):
    state, copy = copy_flow
    capture = cn.ContactNumberReader._capture
    def timeout(window, **kw):
        if kw.get("after_100ns") is not None:
            raise RuntimeError("QQ_CONTACT_NUMBER_WGC_TIMEOUT")
        return capture(window, **kw)
    monkeypatch.setattr(cn.ContactNumberReader, "_capture", staticmethod(timeout))
    with pytest.raises(RuntimeError, match="QQ_CONTACT_NUMBER_WGC_TIMEOUT"):
        copy()
    assert [click[0] for click in state.clicks] == ["right"]
    assert state.closed == [("esc",)]


def test_capture_generation_change_stops_even_when_pixels_are_identical(monkeypatch):
    from utils.core.screenshot import CapturedFrame, WgcFrameInfo
    reader = cn.ContactNumberReader()
    frame = CapturedFrame(Image.new("RGB", (40, 30)), WgcFrameInfo())
    frame.info.generation = 1
    monkeypatch.setattr(cn, "WGCCapture", lambda: SimpleNamespace(capture_frame=lambda *a, **kw: frame))
    window = SimpleNamespace(_hWnd=1)
    reader._capture(window)
    frame.info.generation = 2
    with pytest.raises(RuntimeError, match="QQ_CONTACT_NUMBER_WINDOW_CHANGED"):
        reader._capture(window)


@pytest.fixture
def clipboard(monkeypatch):
    state = SimpleNamespace(value="old", sequence=1, owner=8, text=True)
    monkeypatch.setattr(cb.win32clipboard, "OpenClipboard", lambda: None)
    monkeypatch.setattr(cb.win32clipboard, "CloseClipboard", lambda: None)
    monkeypatch.setattr(cb.win32clipboard, "EmptyClipboard", lambda: None)
    def set_value(fmt, value):
        state.value = value
        state.sequence += 1
    monkeypatch.setattr(cb.win32clipboard, "SetClipboardData", set_value)
    monkeypatch.setattr(cb.win32clipboard, "GetClipboardSequenceNumber", lambda: state.sequence)
    monkeypatch.setattr(cb.win32clipboard, "GetClipboardData", lambda fmt: state.value)
    monkeypatch.setattr(cb.win32clipboard, "GetClipboardOwner", lambda: state.owner)
    monkeypatch.setattr(cb.win32clipboard, "IsClipboardFormatAvailable", lambda fmt: state.text)
    monkeypatch.setattr(cb.win32process, "GetWindowThreadProcessId", lambda owner: (0, owner))
    return state


def test_clipboard_requires_new_sequence_and_can_copy_same_number_twice(clipboard):
    for _ in range(2):
        copy = cb.ClipboardCopy()
        copy.mark()
        with pytest.raises(cb.ClipboardCopyError, match="CLIPBOARD_UNCHANGED"):
            copy.read(timeout=.001)
        clipboard.value = "800888598"
        # 新文本但旧序列仍拒绝
        with pytest.raises(cb.ClipboardCopyError, match="CLIPBOARD_UNCHANGED"):
            copy.read(timeout=.001)
        clipboard.sequence += 1
        assert copy.read(expected_pid=8) == "800888598"


def test_clipboard_rejects_other_process_and_non_text(clipboard):
    copy = cb.ClipboardCopy()
    copy.mark()
    clipboard.sequence += 1
    clipboard.value = "800888598"
    with pytest.raises(cb.ClipboardCopyError, match="CLIPBOARD_OWNER_CHANGED"):
        copy.read(expected_pid=9)
    clipboard.text = False
    with pytest.raises(cb.ClipboardCopyError, match="CLIPBOARD_TEXT_UNAVAILABLE"):
        copy.read()


def test_unique_template_rejects_two_independent_buttons():
    import numpy as np
    template = Image.fromarray(np.random.default_rng(0).integers(0, 256, (20, 30, 3), dtype=np.uint8))
    frame = Image.new("RGB", (200, 150), "white")
    assert find_unique_template(frame, template) is None
    frame.paste(template, (10, 20))
    assert find_unique_template(frame, template, threshold=.75)["left"] == 10
    frame.paste(template, (110, 90))
    with pytest.raises(TemplateAmbiguousError):
        find_unique_template(frame, template, threshold=.75)


def test_avatar_ignores_outer_background_but_rejects_identity_change():
    before = Image.new("RGB", (50, 50), "white")
    after = before.copy()
    after.putpixel((0, 0), (128, 128, 128))
    assert cn._same_avatar(before, after)
    after.paste("black", (15, 15, 35, 35))
    assert not cn._same_avatar(before, after)


def test_absent_copy_item_skips_without_clipboard_or_left_click(copy_flow):
    state, copy = copy_flow
    state.missing = True
    assert copy() is None
    assert [c[0] for c in state.clicks] == ["right"]
    assert state.closed == [("esc",)]


def test_identification_uses_single_consumer_queue(monkeypatch):
    import queue
    import threading
    from utils.qq.dispatcher import Dispatcher
    from utils.qq import template_runtime
    monkeypatch.setattr(template_runtime, "operation_scope", lambda: nullcontext())
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def identify(self):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return "800888598"
    monkeypatch.setattr(cn.ContactNumberReader, "identify_current", identify)
    dispatcher = object.__new__(Dispatcher)
    dispatcher.task_queue = queue.Queue()
    dispatcher.stop_event = threading.Event()
    results = [queue.Queue(), queue.Queue()]
    dispatcher.worker_thread = threading.Thread(target=dispatcher.worker_loop)
    dispatcher.worker_thread.start()
    try:
        for result in results:
            dispatcher.task_queue.put(("identify_current_contact", (), {}, result))
        assert entered.wait(2)
        assert len(calls) == 1
        release.set()
        assert [r.get(timeout=2) for r in results] == [({"qqNumber": "800888598"}, None)] * 2
        assert len(calls) == 2 and len(set(calls)) == 1
    finally:
        release.set()
        dispatcher.stop()


LIGHT_ACTIVE, LIGHT_HOVER, LIGHT_IDLE = (226, 226, 226), (235, 235, 235), (245, 245, 245)


def _list_region(backgrounds, monkeypatch):
    """构造只有行背景色差异的假列表，用于验证激活行判定。"""
    width, band = 200, 40
    image = Image.new("RGB", (width, band * len(backgrounds)), LIGHT_IDLE)
    rows = []
    for index, color in enumerate(backgrounds):
        top = index * band
        image.paste(Image.new("RGB", (width, band), color), (0, top))
        rows.append(((30, top + band // 2, 10), (0, top, width, top + band)))
    monkeypatch.setattr(cn.UserList, "crop_user_row_by_avatar", staticmethod(lambda _image: rows))
    rect = dict(left=0, top=0, right=width, bottom=band * len(backgrounds))
    return RegionResult(rect, rect, image, image)


def test_active_row_keeps_band_result_when_another_row_is_hovered(monkeypatch):
    """实测：激活行 226 与鼠标悬停行 235 同时离群，必须仍认定激活行，不报歧义。"""
    region = _list_region([LIGHT_ACTIVE, LIGHT_HOVER] + [LIGHT_IDLE] * 9, monkeypatch)
    assert cn._active_row(region).avatar[1] == 20


def test_active_row_rejects_two_rows_inside_active_band(monkeypatch):
    region = _list_region([LIGHT_ACTIVE, LIGHT_ACTIVE] + [LIGHT_IDLE] * 9, monkeypatch)
    with pytest.raises(RuntimeError, match="QQ_CONTACT_NUMBER_ACTIVE_AMBIGUOUS"):
        cn._active_row(region)


def test_active_row_reports_not_found_without_any_candidate(monkeypatch):
    region = _list_region([LIGHT_IDLE] * 11, monkeypatch)
    with pytest.raises(RuntimeError, match="QQ_CONTACT_NUMBER_ACTIVE_NOT_FOUND"):
        cn._active_row(region)


def test_active_row_falls_back_to_relative_outlier_for_unknown_theme(monkeypatch):
    """未收录的主题（两个灰阶都不在绝对色带内）仍按单一离群行认定激活行。"""
    region = _list_region([(60, 60, 60)] + [(80, 80, 80)] * 5, monkeypatch)
    assert cn._active_row(region).avatar[1] == 20
