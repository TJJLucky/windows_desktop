"""激活行背景判定边界和并列颜色规则。"""
import numpy as np
import pytest
from PIL import Image

from utils.qq.user_list import UserList


@pytest.mark.parametrize("color,expected", [
    ((222, 222, 222), True),
    ((230, 230, 230), True),
    ((221, 226, 226), False),
    ((226, 231, 226), False),
    ((226, 226, 241), False),
    ((241, 241, 241), False),
    ((249, 249, 249), False),
    ((255, 255, 255), False),
])
def test_background_color_boundaries(color, expected):
    assert UserList.is_active_bg(Image.new("RGB", (30, 10), color)) is expected


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
