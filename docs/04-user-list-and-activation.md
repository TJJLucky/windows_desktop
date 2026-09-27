# 用户列表识别与激活

## 用户键

`UserList.users` 的 key 使用 QQ 用户列表 OCR 得到的名字。聊天记录也使用同一个名字作为联系人键，不再把请求中的任意别名直接当作稳定身份。

## 列表刷新

`UserList.refresh()`：

1. 调用 `refresh_image()` 等待窗口和列表滚动稳定并获取整窗截图。
2. 用模板定位搜索栏，得到列表区域。
3. 用霍夫圆检测头像圆，按 `cy` 排序。
4. 把所有行的文字区域垂直拼图，一次 OCR 识别昵称。
5. 用 `split_ocr_by_bubble()` 把 OCR 行还原到各个用户行。
6. 同时 OCR 列表右上角，得到当前会话名。

## Key 的更新

右上角 OCR 是当前会话的完整名字。每次刷新或激活时：

- 如果列表行 OCR 名字命中右上角名字，则用右上角名字作为 `User.name` 和 `users` key。
- 激活完成后再次读取右上角 OCR，并把当前用户从短名字 key 提升为完整名字 key。
- 旧 key 不再保留，不建立 alias，也不迁移旧聊天记录。
## 激活判定

是否已激活以列表右上角 OCR 为准，不以行背景色为准。点击前根据顶部 OCR 的目标名字决定是否需要点击。

## 点击坐标

点击使用整窗截图中的头像圆坐标，点击前加上截图时窗口的屏幕原点：

```text
origin_x = screen_region.left - image_region.left
origin_y = screen_region.top  - image_region.top
click_x  = origin_x + circle_x + radius + 5
click_y  = origin_y + circle_y
```

列表头像圆按当前列表区域的 `left/right/top` 动态过滤，不使用固定像素阈值。

## 验证与重试

点击在 `WindowCaptureCtx` 中执行，临时置顶但不抢焦点。点击后重新截图，通过 `get_user_list_top_right_ocr()` 轮询目标用户名；失败时重新刷新坐标并重试一次，仍未确认则显式失败。