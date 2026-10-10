# 消息读取与失败语义

## 读取流程

`MessageList.read_messages(contact_name)` 是消息读取的唯一入口：

1. 通过 `UserList.active_user_by_name()` 激活目标会话；联系人定位仍会使用用户列表 OCR。
2. 通过消息区模板确定安全的框选范围。
3. 从内缩后的左上按住左键拖到右下，进入 QQ 多选状态。
4. 优先匹配 `templates/copy_icon.png`；工具栏图标模板不匹配时，回退到底部 ROI 的
   OCR 精确文字“复制”，再按文字标签定位其上方的图标点击区域。
5. 读取 QQ 写入系统剪贴板的 Unicode 文本。
6. 按 `发送人: MM-DD HH:MM:SS 正文` 解析为 `sender`、`timestamp`、`text`、`rawText`；
   私聊/服务号仅复制正文时，用当前联系人 key 补 `sender`，`timestamp` 保持为空。

消息正文不再使用气泡检测、拼图或 OCR，也不返回气泡坐标或方向。智能体应根据
`sender` 与自身 QQ 名称自行判断消息归属。

## 失败语义

框选、复制图标定位、剪贴板读取或复制文本格式解析失败时，`MessageList` 抛出明确的
`RuntimeError` 错误码。上层将其投影为 `QQ_MESSAGES_UNAVAILABLE` 与 HTTP 503。

复制操作会改变系统剪贴板；服务只在确认复制动作存在后写入唯一哨兵，并只接受本次
复制后出现的非空 Unicode 文本。复制动作定位失败时不会提前覆盖用户原剪贴板。
