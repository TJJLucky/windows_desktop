from utils.core import mouse


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
