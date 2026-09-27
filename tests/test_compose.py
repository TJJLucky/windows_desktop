"""拼图 OCR（compose）单元测试：拼图尺寸/间隔与 y 边界切分逻辑。

不依赖真实 OCR 模型：只验证拼图几何与文本归属的纯逻辑。
"""

# PIL.Image / ImageDraw：构造假气泡图（纯色底 + 黑字块）
from PIL import Image, ImageDraw

# 被测函数
from utils.vision.compose import compose_bubbles_to_one, split_ocr_by_bubble


def make_bubble(width: int, height: int, color=(255, 255, 255)):
    """构造一个纯色底假气泡图（RGB）。"""
    img = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(img)
    # 左上角画一个黑色文字块（模拟文本，不参与断言细节）
    draw.rectangle((4, 4, 40, height - 4), fill=(0, 0, 0))
    return img


def test_compose_canvas_width_is_max_without_stretch():
    """场景：两个不同尺寸气泡 → 画布宽=最大宽，气泡保持原始尺寸不拉伸，间隔正确。"""
    # 气泡 A：宽 100 高 30；气泡 B：宽 200 高 50
    composed, bounds = compose_bubbles_to_one([
        make_bubble(100, 30),
        make_bubble(200, 50),
    ])
    # 断言：画布宽度为最大宽 200（气泡 A 不拉伸，靠左留白）
    assert composed.width == 200
    # 断言：总高 = 30 + 50 + 4（一个间隔）
    assert composed.height == 30 + 50 + 4
    # 断言：A 区间 [0,30)，B 区间 [34,84)（间隔 4px 在两者之间）
    assert bounds == [(0, 30), (34, 84)]


def test_compose_empty_returns_blank():
    """场景：空气泡列表 → 返回 1x1 白图与空区间（不崩溃）。"""
    composed, bounds = compose_bubbles_to_one([])
    # 断言：尺寸 1x1，区间为空
    assert composed.size == (1, 1)
    assert bounds == []


def test_split_ocr_by_bubble_assigns_by_y():
    """场景：OCR 行 y 中心分别落在两个气泡区间 → 文本正确归属。"""
    # 两个气泡的 y 区间（与拼图一致）
    bounds = [(0, 30), (34, 84)]
    # 假 OCR 行：第一条 y 中心=10（气泡0），第二条 y 中心=50（气泡1）
    lines = [
        ([[0, 5], [100, 5], [100, 15], [0, 15]], "第一条", 0.9),
        ([[0, 45], [100, 45], [100, 55], [0, 55]], "第二条", 0.9),
    ]
    texts = split_ocr_by_bubble(lines, bounds)
    # 断言：气泡0="第一条"，气泡1="第二条"
    assert texts == ["第一条", "第二条"]


def test_split_ocr_by_bubble_multiline_concatenates():
    """场景：同一气泡内多行 OCR → 直接拼接（无分隔符，保持原语义）。"""
    # 单气泡区间
    bounds = [(0, 50)]
    # 同一气泡内的两行（y 中心都在 0~50）
    lines = [
        ([[0, 5], [100, 5], [100, 15], [0, 15]], "第一行", 0.9),
        ([[0, 30], [100, 30], [100, 40], [0, 40]], "第二行", 0.9),
    ]
    texts = split_ocr_by_bubble(lines, bounds)
    # 断言：直接拼接为"第一行第二行"
    assert texts == ["第一行第二行"]


def test_split_ocr_by_bubble_empty_lines():
    """场景：无 OCR 结果 → 返回与气泡数等长的空串列表。"""
    # 两个气泡区间
    bounds = [(0, 30), (34, 84)]
    # 空 OCR 结果
    texts = split_ocr_by_bubble([], bounds)
    # 断言：两个空串
    assert texts == ["", ""]


def test_split_ocr_by_bubble_restores_scaled_y():
    """OCR 输入放大 1.25 倍后，y 坐标应还原到 bounds 原图坐标系。"""
    bounds = [(0, 30), (34, 84)]
    # 原图 y 中心 29，放大 1.25 后为 36.25；若不还原会被错分到第二个气泡。
    lines = [([[0, 36.25], [100, 36.25], [100, 36.25], [0, 36.25]], "第一条", 0.9)]
    texts = split_ocr_by_bubble(lines, bounds, scale=1.25)
    assert texts == ["第一条", ""]
