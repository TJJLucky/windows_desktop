# 聊天记录方案

## 联系人键

聊天记录以用户列表中的原始名字作为 `contact_name` 分区键。请求方应使用
`/v1/contacts:query` 返回的 key 原样传回。

## 存储结构

`service/chat_history.py` 使用两类表：

- `messages`：正式聊天记录，保存 `sender`、`timestamp`、`text`、`rawText` 与 `seq`。
- `contact_snapshot`：每个联系人上一次复制到的可见消息窗口快照。

不保存 `direction`，也不根据消息文本猜测收发方向或关联发送命令。调用智能体应根据
每条消息的 `sender` 与自己的 QQ 名称判断消息归属。

## 窗口差异追加

`append_visible(contact_name, messages)`：

1. 读取上一次快照。
2. 以 `timestamp + sender + text` 对齐上一次窗口和本次窗口。
3. 无法直接连续对齐时使用 LCS 做顺序对齐。
4. 只追加当前窗口中新出现的尾部消息。
5. 保存当前窗口为新快照。

这样可以避免同一窗口重复复制时重复入库，也能在滚动后的重叠窗口中只追加新增消息。

## 发送与历史

`/v1/commands/send` 的幂等状态由 `command_ledger` 独立维护。发送成功不会立即伪造一条
聊天记录；只有后续 QQ 原生复制读取到的消息才会进入 `messages`。

## 分页

`get_history()` 返回最近一页，再按 `seq` 升序输出。
