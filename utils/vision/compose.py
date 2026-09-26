"""视觉编排：截图拼接与批量 OCR（拼图一次识别）。

把同属一个区域的多个小图（如聊天气泡）垂直拼接成一张大图，
一次 OCR 出全部文本，再按拼图时的 y 边界把识别行切分回各小图。
相比"每张小图单独 OCR"，把 N 次模型推理合并为 1 次，显著降低总耗时。

依赖方向：仅依赖 PIL（core/vision 层内无循环依赖）。
"""

# PIL.Image：新建画布/粘贴气泡；ImageDraw：画分隔线
from PIL import Image, ImageDraw

# 拼图时相邻气泡之间的间隔高度（分隔线厚度，像素）
BUBBLE_GAP = 4


def compose_bubbles_to_one(bubbles: list[Image.Image]) -> tuple[Image.Image, list[tuple[int, int]]]:
    """把多个气泡小图垂直拼接为一张大图，返回 (大图, 各气泡的 y 区间)。

    规则：
      - 画布宽度 = 最大气泡宽度，但每个气泡**保持原始宽高**靠左粘贴（不拉伸、
        不缩放，右侧留白）——拉伸文字会改变字形导致 OCR 识别错误；
      - 气泡间插入 BUBBLE_GAP 像素的黑色分隔线，防止跨气泡粘连，并给出明确的 y 边界；
      - 返回的 y 区间 [(top, bottom), ...] 对应每个气泡在拼图中的纵向范围（不含间隔区）。

    :param bubbles: 气泡小图列表（RGB，顺序与业务列表一致）
    :return: (拼接好的大图, 各气泡的 (top, bottom) 列表)
    """
    # 空列表：返回 1x1 白图 + 空区间（调用方据此跳过组装）
    if not bubbles:
        return Image.new("RGB", (1, 1), (255, 255, 255)), []
    # 画布宽度 = 最大气泡宽度（仅用于画布尺寸，不拉伸任何气泡）
    width = max(b.width for b in bubbles)
    # 总高度 = 各气泡原始高度之和 + 间隔数 × 间隔高
    total_h = sum(b.height for b in bubbles) + BUBBLE_GAP * (len(bubbles) - 1)
    # 白底画布（气泡背景为白/浅色，分隔线为黑，对比清晰）
    canvas = Image.new("RGB", (width, total_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    bounds: list[tuple[int, int]] = []
    y = 0
    # 逐气泡：按原始尺寸靠左粘贴 → 记录区间 → 画分隔线（最后一个不画）
    for i, bubble in enumerate(bubbles):
        # 保持原始宽高粘贴（靠左，右侧留白；不做任何缩放）
        canvas.paste(bubble, (0, y))
        # 记录该气泡的纵向区间
        bounds.append((y, y + bubble.height))
        # 移动到气泡底端
        y += bubble.height
        # 非最后一个：画黑色分隔线并前移间隔高度
        if i < len(bubbles) - 1:
            draw.line([(0, y), (width, y)], fill=(0, 0, 0), width=BUBBLE_GAP)
            y += BUBBLE_GAP
    return canvas, bounds


def split_ocr_by_bubble(lines: list, bounds: list[tuple[int, int]]) -> list[str]:
    """把一次 OCR 的识别行按 y 中心归属到各气泡，返回每个气泡的文本。

    :param lines: OCR 结果 [[bbox, text, confidence], ...]；bbox 为四点坐标 [[x,y],...]
    :param bounds: compose_bubbles_to_one 返回的各气泡 y 区间 [(top, bottom), ...]
    :return: 与 bounds 等长的文本列表（每个气泡一行文本，多行直接拼接，无分隔符）
    """
    # 结果数组：长度与气泡数一致，初始为空串
    texts = [""] * len(bounds)
    # 空 OCR 结果 → 全部空串
    if not lines:
        return texts
    # 提取每个识别行的 y 中心（bbox 四点的平均 y）
    rows: list[tuple[float, str]] = []
    for bbox, text, _conf in lines:
        ys = [pt[1] for pt in bbox]  # 四个角的 y 坐标
        center_y = sum(ys) / len(ys)  # 行中心 y
        rows.append((center_y, text))
    # 按 y 中心把每行归入对应的气泡区间（与拼接顺序一致）
    for center_y, text in rows:
        for idx, (top, bottom) in enumerate(bounds):
            # 行中心落在该气泡区间内 → 归属该气泡
            if top <= center_y < bottom:
                # 多行直接拼接（保持与原逐气泡 OCR 相同的文本语义）
                texts[idx] += text
                break
    return texts
