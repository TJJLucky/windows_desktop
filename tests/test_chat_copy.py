from utils.qq.message import _find_bottom_copy_action, _find_copy_menu_point, _normalise_menu_text


def test_normalise_menu_text_removes_ocr_whitespace():
    assert _normalise_menu_text(" 复\n制 ") == "复制"


def test_find_copy_menu_point_uses_nearest_exact_copy_label():
    ocr_lines = [
        ([[10, 10], [40, 10], [40, 30], [10, 30]], "复制", 0.99),
        ([[150, 150], [180, 150], [180, 170], [150, 170]], "复制", 0.99),
        ([[200, 200], [250, 200], [250, 220], [200, 220]], "复制内容", 0.99),
    ]

    point = _find_copy_menu_point(ocr_lines, 100, 200, (270, 370))

    assert point == (265, 360)


def test_find_copy_menu_point_returns_none_without_exact_label():
    assert _find_copy_menu_point([([[-1, -1]] * 4, "复制内容", 0.99)], 0, 0, (0, 0)) is None


def test_find_bottom_copy_action_ignores_chat_text_and_uses_bottom_toolbar():
    ocr_lines = [
        ([[200, 100], [240, 100], [240, 120], [200, 120]], "复制", 0.99),
        ([[900, 850], [940, 850], [940, 870], [900, 870]], "复制", 0.99),
    ]

    assert _find_bottom_copy_action(ocr_lines, 0, 0, 1000) == (920, 860)
