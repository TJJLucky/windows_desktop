# 聊天记录方案

## 联系人键

聊天记录以用户列表中的原始名字作为 `contact_name` 分区键。请求方应使用 `/v1/contacts:query` 返回的 key 原样传回。

当前不建立 QQ 账号 ID 映射，也不做历史兼容别名。右上角 OCR 更新用户 key 后，新消息使用新 key；旧 key 下的历史记录不会自动迁移或合并。

## 存储结构

`service/chat_history.py` 使用三类表：

- `messages`：正式聊天记录，按 `contact_name` 和 `seq` 排序。
- `contact_snapshot`：每个联系人的上一次可见窗口快照。
- `pending_outbound`：已经发送、但还没有在可见窗口确认的外发消息。

## 窗口差异追加

`append_visible(contact_name, messages)`：

1. 读取上一次快照。
2. 优先寻找上一次窗口尾部与本次窗口头部的最长连续重叠。
3. 无法直接重叠时使用 LCS 做顺序对齐。
4. 只追加当前窗口中新增的尾部消息。
5. 保存当前窗口为新快照。

这样处理：

- 相同文本连续出现不会被错误合并。
- 同一窗口重复读取不会重复入库。
- 滚动窗口后只追加新增尾部。

## 外发消息

发送成功先调用 `mark_outbound()`。后续可见窗口出现该消息时：

- 按方向和文本匹配最早的 pending 记录。
- 将 `commandId` 写入正式消息。
- 删除 pending 记录。

## 分页

`get_history()` 返回最近一页，再按 `seq` 升序输出，避免历史超过一页后只能看到最旧记录。