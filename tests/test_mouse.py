from utils.core import mouse
import pytest


def test_input_event_dll_is_loaded_from_project_libs():
    assert mouse._dll_path.name == "input_event.dll"
    assert mouse._dll_path.is_file()
    assert mouse._input_event.RmouseDown
    assert mouse._input_event.RmouseUp


def test_random_point_rejects_empty_region():
    try:
        mouse.random_point(0, 0, 0, 10)
    except ValueError:
        pass
    else:
        raise AssertionError("空区域必须拒绝")


def test_input_event_coordinate_limit_is_explicit():
    try:
        mouse._require_screen_coordinate(32769, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("超出 DLL 范围的坐标必须拒绝")


def test_scroll_at_preserves_signed_wheel_delta_and_restores_cursor(monkeypatch):
    calls = []
    monkeypatch.setattr(mouse, "_cursor_position", lambda: (10, 20))
    monkeypatch.setattr(mouse, "move_to", lambda *args: calls.append(("move", args)))
    monkeypatch.setattr(mouse, "_call", lambda *args: calls.append(args))
    monkeypatch.setattr(mouse.time, "sleep", lambda seconds: None)
    mouse.scroll_at(300, 400, -120)
    assert calls == [("move", (300, 400)), ("scrollUp", -120), ("move", (10, 20))]


def test_scroll_at_restores_cursor_on_input_failure(monkeypatch):
    import pytest
    moves = []
    monkeypatch.setattr(mouse, "_cursor_position", lambda: (10, 20))
    monkeypatch.setattr(mouse, "move_to", lambda *args: moves.append(args))
    monkeypatch.setattr(mouse.time, "sleep", lambda seconds: None)
    def fail(*args):
        raise RuntimeError("wheel failed")
    monkeypatch.setattr(mouse, "_call", fail)
    with pytest.raises(RuntimeError, match="wheel failed"):
        mouse.scroll_at(300, 400, -120)
    assert moves == [(300, 400), (10, 20)]


def test_direct_click_waits_for_actual_cursor_without_smooth_motion(monkeypatch):
    calls = []
    positions = iter([(10, 20), (300, 400)])
    monkeypatch.setattr(mouse, "_cursor_position", lambda: next(positions))
    monkeypatch.setattr(mouse, "move_to", lambda *args: calls.append(("move", args)))
    monkeypatch.setattr(mouse, "smooth_move_to", lambda *args: (_ for _ in ()).throw(AssertionError("smooth")))
    monkeypatch.setattr(mouse, "_call", lambda *args: calls.append(args))
    monkeypatch.setattr(mouse.time, "sleep", lambda seconds: calls.append(("wait", seconds)))
    mouse.right_click_at(300, 400, restore=False, smooth=False)
    assert calls == [("move", (300, 400)), ("wait", .005), ("RmouseDown",), ("wait", .05), ("RmouseUp",)]


def test_direct_click_does_not_press_when_cursor_never_arrives(monkeypatch):
    import pytest
    calls = []
    times = iter([0, .01, .3])
    monkeypatch.setattr(mouse.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(mouse.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(mouse, "_cursor_position", lambda: (10, 20))
    monkeypatch.setattr(mouse, "move_to", lambda *args: None)
    monkeypatch.setattr(mouse, "_call", lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match="MOUSE_POSITION_UNCONFIRMED"):
        mouse.click_at(300, 400, restore=False, smooth=False)
    assert calls == []


@pytest.mark.parametrize("function,down,up", [
    (mouse.click_at, "LmouseDown", "LmouseUp"),
    (mouse.right_click_at, "RmouseDown", "RmouseUp"),
    (mouse.random_click, "LmouseDown", "LmouseUp"),
    (mouse.random_right_click, "RmouseDown", "RmouseUp"),
])
def test_default_clicks_move_directly_and_restore(monkeypatch, function, down, up):
    calls = []
    positions = iter([(10, 20), (10, 20), (300, 400)])
    monkeypatch.setattr(mouse, "_cursor_position", lambda: next(positions))
    monkeypatch.setattr(mouse, "move_to", lambda *args: calls.append(("move", args)))
    monkeypatch.setattr(mouse, "smooth_move_to", lambda *args: pytest.fail("default used WindMouse"))
    monkeypatch.setattr(mouse, "random_point", lambda *args: (300, 400))
    monkeypatch.setattr(mouse, "_call", lambda *args: calls.append(args))
    monkeypatch.setattr(mouse.time, "sleep", lambda seconds: calls.append(("wait", seconds)))
    args = (300, 400, 10, 10) if function in (mouse.random_click, mouse.random_right_click) else (300, 400)
    function(*args)
    assert calls == [("move", (300, 400)), ("wait", .005), (down,), ("wait", .05),
                     (up,), ("move", (10, 20))]


def test_smooth_click_is_still_explicitly_available(monkeypatch):
    calls = []
    monkeypatch.setattr(mouse, "_cursor_position", lambda: (10, 20))
    monkeypatch.setattr(mouse, "smooth_move_to", lambda *args: calls.append(("smooth", args)))
    monkeypatch.setattr(mouse, "move_to", lambda *args: pytest.fail("unexpected direct move"))
    monkeypatch.setattr(mouse, "_call", lambda *args: calls.append(args))
    monkeypatch.setattr(mouse.time, "sleep", lambda seconds: None)
    mouse.click_at(300, 400, smooth=True)
    assert calls == [("smooth", (300, 400)), ("LmouseDown",), ("LmouseUp",), ("smooth", (10, 20))]
