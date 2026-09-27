# 拼图 OCR 与坐标还原

## 目标

把多个小区域垂直拼成一张图，一次 OCR 输出全部文本，再按 y 区间切回每个原始区域，减少重复模型推理。

## 拼图规则

`compose_bubbles_to_one()`：

- 画布宽度取最大区域宽度。
- 每个区域保持原始宽高，不拉伸、不缩放。
- 区域之间插入黑色分隔线。
- 返回每个区域在原拼图中的 `(top, bottom)`。

## 切分规则

`split_ocr_by_bubble(lines, bounds, scale=1.0)`：

- OCR 坐标的 y 中心默认与 `bounds` 同一坐标系。
- 如果 OCR 前对图做了放大，必须传入 `scale` 参数。
- 行中心会执行：

```text
center_y = ocr_center_y / scale
```

否则放大后的 OCR 坐标会被错误分到下一行。

## 用户列表

`UserList.ocr_preprocess()` 默认放大 1.25 倍，因此 `UserList.refresh()` 必须调用：

```python
split_ocr_by_bubble(lines, bounds, scale=1.25)
```

## 消息列表

`MessageList` 直接把拼图交给 OCR，不进行额外放大，所以使用默认：

```python
split_ocr_by_bubble(lines, bounds)
```

## 参数保护

`scale <= 0` 会抛出 `ValueError`，避免除零或反向坐标。