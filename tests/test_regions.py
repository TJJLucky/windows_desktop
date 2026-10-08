"""QQ 区域定位回退测试。"""

from pathlib import Path

from PIL import Image

from utils.qq.regions import _fallback_user_list_region, find_template, load_image


def test_debug_qq_image_uses_fixed_60px_user_list_fallback():
    image_path = Path(__file__).parents[1] / "debug" / "test.jpg"
    assert image_path.exists(), "需要用户提供的 debug/test.jpg 回归图片"
    image = Image.open(image_path).convert("RGB")
    left = find_template(image, load_image("search_bar_left.png"), 0.9)
    right = find_template(image, load_image("search_bar_right.png"), 0.9)

    assert left is None
    assert right is not None
    assert _fallback_user_list_region(image, right) == {
        "x": 60, "y": 56, "top": 56, "bottom": 1031,
        "left": 60, "right": 238,
    }
