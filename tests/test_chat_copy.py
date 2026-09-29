from types import SimpleNamespace

from PIL import Image

from utils.qq import regions


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
