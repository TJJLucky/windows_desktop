# 发送与命令幂等

## 外部发送流程

`POST /v1/commands/send` 请求包含：

- `commandId`：调用方生成的稳定幂等键。
- `contactName`：用户列表中的联系人名字。
- `text`：最多 5000 字符的消息文本。

## 命令账本状态

`CommandLedger` 状态：

- `RUNNING`：已经占位，正在执行。
- `SUCCEEDED`：发送完成。
- `FAILED`：发送失败。
- `EFFECT_UNKNOWN`：进程重启或结果不确定，不能自动重发。

相同 `commandId` 和相同消息体只执行一次；相同 ID 配不同消息体返回 409。

## 发送链路

```text
HTTP /v1/commands/send
  -> CommandLedger.begin
  -> Dispatcher 单消费者队列
  -> InputBox.send_text
  -> UserList 激活
  -> 剪贴板粘贴 + Enter
  -> CommandLedger.resolve
```

## 聊天记录关联

发送命令状态由 `CommandLedger` 独立保存。聊天历史不根据发送正文猜测或伪造记录；只有后续
`/v1/commands/read` 从 QQ 原生复制到的消息才会写入正式历史。消息归属由调用智能体根据
复制结果中的 `sender` 自行判断。

## 幂等原则

- `commandId` 是调用方提供的稳定键，不能用随机值代替。
- 结果不确定时不自动重发。
- 发送日志只记录联系人、命令 ID、文本长度和状态，不记录完整消息正文。
