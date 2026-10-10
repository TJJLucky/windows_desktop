from types import SimpleNamespace

from PIL import Image
import pytest

from utils.qq.message import (
    _MESSAGE_SELECTION_DRAG_DURATION,
    _is_empty_message_region,
    _selection_ui_changed,
)
from utils.qq import regions
from utils.core import mouse


def test_get_copy_action_region_matches_only_bottom_toolbar(monkeypatch):
    """模板匹配只看窗口下方，并将 ROI 相对坐标还原为屏幕绝对坐标。"""
    captured: dict[str, object] = {}

    def fake_find_template(big, small, threshold, use_mask=False, multiscale=True):
        captured["roi_size"] = big.size
        captured["threshold"] = threshold
        captured["multiscale"] = multiscale
        return {"x": 300, "y": 40, "left": 300, "top": 40, "right": 340, "bottom": 80}

    monkeypatch.setattr(regions, "find_template", fake_find_template)
    monkeypatch.setattr(regions, "load_image", lambda _: Image.new("RGB", (40, 40), "white"))
    window = SimpleNamespace(left=100, top=200)
    full_image = Image.new("RGB", (1000, 800), "white")

    result = regions.get_copy_action_region(window, full_image)

    assert captured == {"roi_size": (1000, 280), "threshold": 0.88, "multiscale": False}
    assert result == {
        "x": 400,
        "y": 760,
        "w": 40,
        "h": 40,
        "left": 400,
        "top": 760,
        "right": 440,
        "bottom": 800,
    }


def test_get_copy_action_region_returns_none_when_template_is_not_found(monkeypatch):
    monkeypatch.setattr(regions, "find_template", lambda *args, **kwargs: None)
    monkeypatch.setattr(regions, "load_image", lambda _: Image.new("RGB", (40, 40), "white"))

    result = regions.get_copy_action_region(
        SimpleNamespace(left=0, top=0),
        Image.new("RGB", (600, 600), "white"),
    )

    assert result is None


def test_find_copy_text_region_prefers_special_text_toolbar_row():
    """特殊会话没有复制图标时，应识别同一工具栏行里的文字按钮“复制”。"""
    def line(x1, y1, x2, y2, value):
        return [[[x1, y1], [x2, y1], [x2, y2], [x1, y2]], value, 1.0]

    ocr_lines = [
        line(483, 380, 547, 399, "逐条转发"),
        line(572, 380, 636, 399, "合并转发"),
        line(662, 380, 739, 398, "保存至电脑"),
        line(779, 380, 815, 400, "收藏"),
        line(868, 378, 904, 400, "删除"),
        line(956, 378, 993, 400, "复制"),
    ]

    result = regions._find_copy_text_region(
        ocr_lines,
        image_width=1500,
        image_height=1390,
        toolbar_top=903,
    )

    assert result == {
        "x": 956,
        "y": 1281,
        "w": 37,
        "h": 22,
        "left": 956,
        "top": 1281,
        "right": 993,
        "bottom": 1303,
    }


def test_copy_text_click_region_targets_icon_above_label():
    """OCR 定位文字标签后，点击区域应上移到真正的按钮图标。"""
    label_region = {
        "x": 956,
        "y": 1281,
        "w": 37,
        "h": 22,
        "left": 956,
        "top": 1281,
        "right": 993,
        "bottom": 1303,
    }

    result = regions._copy_text_button_click_region(
        label_region,
        image_width=1500,
        image_height=1390,
    )

    assert result == {
        "x": 954,
        "y": 1221,
        "w": 41,
        "h": 40,
        "left": 954,
        "top": 1221,
        "right": 995,
        "bottom": 1261,
    }


def test_message_selection_drag_keeps_short_verified_track():
    """瞬时移动会偶发只触发点击，0.2 秒轨迹可稳定进入 QQ 框选态。"""
    assert _MESSAGE_SELECTION_DRAG_DURATION == 0.2


def test_empty_message_region_requires_f5f5f5_canvas():
    window = SimpleNamespace(left=0, top=0)
    region = {"left": 100, "top": 120, "right": 900, "bottom": 800, "w": 800, "h": 680}
    empty = Image.new("RGB", (1000, 1000), (245, 245, 245))

    assert _is_empty_message_region(window, empty, region) is True

    non_empty = empty.copy()
    for x in range(180, 520):
        for y in range(260, 420):
            non_empty.putpixel((x, y), (255, 255, 255))
    assert _is_empty_message_region(window, non_empty, region) is False


def test_find_non_resize_x_skips_resize_hit_area_and_restores_cursor(monkeypatch):
    """真实命中探测应越过 SIZEWE 像素，探测后恢复原鼠标位置。"""
    handles = iter([100, 100, 100, 200])
    moves: list[tuple[int, int]] = []
    monkeypatch.setattr(mouse, "_load_resize_cursor_handles", lambda: {100})
    monkeypatch.setattr(mouse, "_cursor_position", lambda: (777, 555))
    monkeypatch.setattr(mouse, "move_to", lambda x, y: moves.append((x, y)))
    monkeypatch.setattr(mouse, "_current_cursor_handle", lambda: next(handles))
    monkeypatch.setattr(mouse.time, "sleep", lambda _: None)

    assert mouse.find_non_resize_x(471, 115, 500) == 474
    assert moves == [(471, 115), (472, 115), (473, 115), (474, 115), (777, 555)]


def test_find_non_resize_x_returns_none_without_normal_cursor(monkeypatch):
    moves: list[tuple[int, int]] = []
    monkeypatch.setattr(mouse, "_load_resize_cursor_handles", lambda: {100})
    monkeypatch.setattr(mouse, "_cursor_position", lambda: (777, 555))
    monkeypatch.setattr(mouse, "move_to", lambda x, y: moves.append((x, y)))
    monkeypatch.setattr(mouse, "_current_cursor_handle", lambda: 100)
    monkeypatch.setattr(mouse.time, "sleep", lambda _: None)

    assert mouse.find_non_resize_x(471, 115, 500, max_search=3) is None
    assert moves == [(471, 115), (472, 115), (473, 115), (777, 555)]


def test_direct_click_skips_windmouse(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(mouse, "_call", lambda name, *args: calls.append((name, *args)))

    mouse.click_at(100, 200, delay=0, restore=False, smooth=False)

    assert calls == [
        ("Mousegoto", 100, 200),
        ("LmouseDown",),
        ("LmouseUp",),
    ]


def test_drag_moves_directly_to_selection_start(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(mouse, "_call", lambda name, *args: calls.append((name, *args)))

    mouse.drag(100, 200, 300, 400, duration=0)

    assert calls[0] == ("Mousegoto", 100, 200)
    assert calls[1][0] == "dragFromTo"
    assert all(call[0] != "SmoothMousegoto" for call in calls)

def test_copy_waits_for_delayed_toolbar_and_uses_threshold_fallback(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from utils.qq import message as message_module

    image = Image.new("RGB", (800, 600), "white")
    window = SimpleNamespace(_hWnd=123, left=0, top=0)
    actions = [None, None, None, {"left": 100, "top": 500, "right": 120, "bottom": 520, "w": 20, "h": 20}]
    thresholds = []
    monkeypatch.setenv("QQ_WINDOW_CACHE_DB", str(tmp_path / "state" / "window.sqlite3"))
    monkeypatch.setattr(message_module, "ensure_qq_window_with_retry", lambda: window)
    monkeypatch.setattr(message_module, "get_qq_window_image", lambda win: (image.copy(), window))
    monkeypatch.setattr(message_module, "get_copy_action_region", lambda win, img, threshold=None: thresholds.append(threshold) or actions.pop(0))
    monkeypatch.setattr(message_module.MessageList, "_refresh_message_region", lambda self, win: {"left": 0, "top": 0, "right": 800, "bottom": 500, "w": 800, "h": 500})
    monkeypatch.setattr(message_module, "find_non_resize_x", lambda x_start, y, x_end: x_start)
    drag_calls = []
    monkeypatch.setattr(message_module, "drag", lambda *args, **kwargs: drag_calls.append((args, kwargs)))
    monkeypatch.setattr(message_module, "click_at", lambda *args, **kwargs: None)
    monkeypatch.setattr(message_module, "_write_clipboard_sentinel", lambda value: None)
    monkeypatch.setattr(message_module, "_read_unicode_clipboard", lambda sentinel: "TJJ 12:00\n有货")
    monkeypatch.setattr(message_module.time, "sleep", lambda value: None)
    monkeypatch.setattr(message_module, "WindowCaptureCtx", _DummyWindowContext)

    assert message_module.MessageList().copy_visible_selection() == "TJJ 12:00\n有货"
    assert drag_calls == [((0, 1, 799, 499), {"duration": 0.2})]
    assert 0.80 in thresholds


def test_copy_reports_specific_error_when_drag_hit_area_is_not_found(monkeypatch, tmp_path):
    from utils.qq import message as message_module

    image = Image.new("RGB", (800, 600), "white")
    window = SimpleNamespace(_hWnd=123, left=0, top=0)
    drag_calls = []
    monkeypatch.setenv("QQ_WINDOW_CACHE_DB", str(tmp_path / "state" / "window.sqlite3"))
    monkeypatch.setattr(message_module, "ensure_qq_window_with_retry", lambda: window)
    monkeypatch.setattr(message_module, "get_qq_window_image", lambda win: (image.copy(), window))
    monkeypatch.setattr(message_module, "get_copy_action_region", lambda *args, **kwargs: None)
    monkeypatch.setattr(message_module.MessageList, "_refresh_message_region", lambda self, win: {"left": 471, "top": 114, "right": 1275, "bottom": 1127, "w": 804, "h": 1013})
    monkeypatch.setattr(message_module, "find_non_resize_x", lambda x_start, y, x_end: None)
    monkeypatch.setattr(message_module, "drag", lambda *args, **kwargs: drag_calls.append((args, kwargs)))
    monkeypatch.setattr(message_module, "WindowCaptureCtx", _DummyWindowContext)

    with pytest.raises(RuntimeError, match="QQ_MESSAGE_SELECTION_START_NOT_FOUND"):
        message_module.MessageList().copy_visible_selection()
    assert drag_calls == []


def test_copy_uses_text_toolbar_fallback_without_single_message_right_click(monkeypatch, tmp_path):
    """多选区域出现文字工具栏后，直接点击“复制”，不进入单条右键复制的降级路径。"""
    from utils.qq import message as message_module

    image = Image.new("RGB", (800, 600), "white")
    window = SimpleNamespace(_hWnd=123, left=0, top=0)
    action = {"left": 310, "top": 540, "right": 350, "bottom": 560, "w": 40, "h": 20}
    clocks = iter([0.0, 0.5, 0.6, 3.0, 3.1])
    clicks: list[tuple] = []
    monkeypatch.setenv("QQ_WINDOW_CACHE_DB", str(tmp_path / "state" / "window.sqlite3"))
    monkeypatch.setattr(message_module, "ensure_qq_window_with_retry", lambda: window)
    monkeypatch.setattr(message_module, "get_qq_window_image", lambda win: (image.copy(), window))
    monkeypatch.setattr(message_module, "get_copy_action_region", lambda *args, **kwargs: None)
    monkeypatch.setattr(message_module, "get_copy_action_region_by_text", lambda *args, **kwargs: action)
    monkeypatch.setattr(message_module.MessageList, "_refresh_message_region", lambda self, win: {"left": 0, "top": 0, "right": 800, "bottom": 500, "w": 800, "h": 500})
    monkeypatch.setattr(message_module, "find_non_resize_x", lambda x_start, y, x_end: x_start)
    monkeypatch.setattr(message_module, "drag", lambda *args, **kwargs: None)
    monkeypatch.setattr(message_module, "click_at", lambda *args, **kwargs: clicks.append((args, kwargs)))
    monkeypatch.setattr(message_module, "_write_clipboard_sentinel", lambda value: None)
    monkeypatch.setattr(message_module, "_read_unicode_clipboard", lambda sentinel: "联系人 12:00\n有货")
    monkeypatch.setattr(message_module.time, "sleep", lambda value: None)
    monkeypatch.setattr(message_module.time, "perf_counter", lambda: next(clocks))
    monkeypatch.setattr(message_module, "WindowCaptureCtx", _DummyWindowContext)

    assert message_module.MessageList().copy_visible_selection() == "联系人 12:00\n有货"
    assert clicks == [((330, 550), {"restore": False, "smooth": False})]


class _DummyWindowContext:
    def __init__(self, hwnd):
        self.hwnd = hwnd
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


def test_parse_failure_has_specific_error(monkeypatch):
    from utils.qq import message as message_module

    monkeypatch.setattr(message_module.UserList, "active_user_by_name", lambda self, name: name)
    monkeypatch.setattr(message_module.MessageList, "copy_visible_selection", lambda self: "无法解析")
    monkeypatch.setattr(message_module, "parse_copied_messages", lambda text: [])

    with pytest.raises(RuntimeError, match="QQ_COPY_PARSE_FAILED"):
        message_module.MessageList().read_messages("联系人")


def test_read_messages_returns_empty_success_for_blank_conversation(monkeypatch):
    from utils.qq import message as message_module

    monkeypatch.setattr(message_module.UserList, "active_user_by_name", lambda self, name: name)
    monkeypatch.setattr(message_module.MessageList, "copy_visible_selection", lambda self: "")

    assert message_module.MessageList().read_messages("联系人") == {
        "copiedText": None,
        "messages": [],
    }


def test_selection_ui_change_detection():
    before = Image.new("RGB", (100, 100), "white")
    unchanged = before.copy()
    changed = before.copy()
    for x in range(20, 80):
        for y in range(80, 95):
            changed.putpixel((x, y), (0, 0, 0))

    assert _selection_ui_changed(before, unchanged) is False
    assert _selection_ui_changed(before, changed) is True


def test_copy_reports_selection_not_entered_when_toolbar_never_appears(monkeypatch, tmp_path):
    from utils.qq import message as message_module

    image = Image.new("RGB", (800, 600), "white")
    window = SimpleNamespace(_hWnd=123, left=0, top=0)
    monkeypatch.setenv("QQ_WINDOW_CACHE_DB", str(tmp_path / "state" / "window.sqlite3"))
    monkeypatch.setattr(message_module, "ensure_qq_window_with_retry", lambda: window)
    monkeypatch.setattr(message_module, "get_qq_window_image", lambda win: (image.copy(), window))
    monkeypatch.setattr(message_module, "get_copy_action_region", lambda *args, **kwargs: None)
    monkeypatch.setattr(message_module, "get_copy_action_region_by_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(message_module.MessageList, "_refresh_message_region", lambda self, win: {"left": 0, "top": 0, "right": 800, "bottom": 500, "w": 800, "h": 500})
    monkeypatch.setattr(message_module, "find_non_resize_x", lambda x_start, y, x_end: x_start)
    monkeypatch.setattr(message_module, "drag", lambda *args, **kwargs: None)
    monkeypatch.setattr(message_module, "_write_clipboard_sentinel", lambda value: None)
    monkeypatch.setattr(message_module.time, "sleep", lambda value: None)
    monkeypatch.setattr(message_module, "WindowCaptureCtx", _DummyWindowContext)

    with pytest.raises(RuntimeError, match="QQ_SELECTION_NOT_ENTERED"):
        message_module.MessageList().copy_visible_selection()


def test_copy_reports_unchanged_clipboard(monkeypatch):
    from utils.qq import message as message_module

    monkeypatch.setattr(message_module, "_read_unicode_clipboard", lambda sentinel: (_ for _ in ()).throw(RuntimeError("QQ_COPY_CLIPBOARD_UNCHANGED")))
    with pytest.raises(RuntimeError, match="QQ_COPY_CLIPBOARD_UNCHANGED"):
        message_module._read_unicode_clipboard("sentinel")
