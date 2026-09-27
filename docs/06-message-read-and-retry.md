# 消息读取与重试

## 读取流程

`MessageList.read_messages(contact_name)`：

1. 通过 `UserList.active_user_by_name()` 激活目标会话。
2. 等待右上角 OCR 确认切换。
3. 调用 `refresh()` 截取消息区域。
4. 检测气泡并拼图。
5. 一次 OCR 识别所有消息。
6. 按气泡 y 范围切分文本并组装 `Message`。

## 重试边界

消息区域定位可能因 QQ 渲染延迟短暂失败。当前只对窗口/区域定位类的 `RuntimeError` 重试一次：

```python
except RuntimeError:
    UserList().active_user_by_name(contact_name)
    self.refresh()
```

OCR 错误、类型错误和其他程序错误不再被吞掉后盲目重试。

## 失败语义

两次失败由上层 `QqAutomationError` 投影为 HTTP 503。重试不会无限循环，也不会掩盖连续异常。