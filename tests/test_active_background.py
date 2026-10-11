"""激活行背景判定边界和并列颜色规则。"""
import numpy as np
import pytest
from PIL import Image

from utils.qq.user_list import UserList


@pytest.mark.parametrize("color,expected", [
    ((222, 222, 222), True),
    ((230, 230, 230), True),
    ((216, 216, 216), True),
    ((234, 234, 234), True),
    ((215, 215, 215), False),
    ((235, 235, 235), False),
    ((113, 113, 113), True),
    ((100, 100, 100), True),
    ((118, 118, 118), True),
    ((99, 99, 99), False),
    ((119, 119, 119), False),
    ((122, 122, 122), False),
    ((221, 226, 226), False),
    ((226, 231, 226), False),
    ((226, 226, 241), False),
    ((241, 241, 241), False),
    ((249, 249, 249), False),
    ((255, 255, 255), False),
])
def test_background_color_boundaries(color, expected):
    assert UserList.is_active_bg(Image.new("RGB", (30, 10), color)) is expected


def test_dark_gray_noise_is_not_filtered_as_shadow():
    """深色主题背景（113/122）必须保留为主体像素，不能被旧阴影规则 (<200) 丢弃。"""
    pixels = np.array([(122, 122, 122)] * 300, dtype=np.uint8).reshape(10, 30, 3)
    kept = UserList.background_pixels(Image.fromarray(pixels))
    assert len(kept) == 300
    assert UserList.is_active_bg(Image.fromarray(pixels)) is False


@pytest.mark.parametrize("backgrounds,expected", [
    ([(113, 113, 113)] + [(122, 122, 122)] * 9, [0]),
    ([(122, 122, 122)] * 9 + [(113, 113, 113)], [9]),
    ([(226, 226, 226)] + [(245, 245, 245)] * 6, [0]),
    ([(122, 122, 122)] * 10, []),
    ([(240, 240, 240)] * 5 + [(226, 226, 226), (230, 230, 230)], [5, 6]),
    ([(113, 113, 113), (226, 226, 226)] + [(122, 122, 122)] * 4, [0, 1]),
])
def test_active_row_index_is_theme_independent(backgrounds, expected):
    assert UserList.active_row_index(backgrounds) == expected


@pytest.mark.parametrize("first,second,expected", [
    ((245, 245, 245), (226, 226, 226), False),
    ((226, 226, 226), (245, 245, 245), True),
])
def test_equal_counts_choose_first_color(first, second, expected):
    pixels = np.array([first] * 150 + [second] * 150, dtype=np.uint8).reshape(10, 30, 3)
    assert UserList.is_active_bg(Image.fromarray(pixels)) is expected


@pytest.mark.parametrize("background_count,expected", [(149, False), (150, True)])
def test_noise_is_filtered_and_minimum_background_count_is_preserved(background_count, expected):
    noise = [(0, 0, 0)] * 200 + [(150, 150, 150)] * 200 + [(199, 199, 199)] * 200
    pixels = np.array(noise + [(226, 226, 226)] * background_count, dtype=np.uint8)
    assert UserList.is_active_bg(Image.fromarray(pixels.reshape(1, -1, 3))) is expected


def test_most_frequent_color_wins_over_first_color():
    pixels = np.array([(245, 245, 245)] * 150 + [(226, 226, 226)] * 151, dtype=np.uint8)
    assert UserList.is_active_bg(Image.fromarray(pixels.reshape(1, -1, 3))) is True
