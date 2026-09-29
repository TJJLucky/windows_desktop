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
