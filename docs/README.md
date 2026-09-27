# Windows Desktop 方案文档

本文档目录记录当前实现的方案边界、关键取舍和运行方式。文档以当前代码为准，业务实现变更时同步更新。

## 阅读顺序

1. [01-exe-only-packaging.md](01-exe-only-packaging.md)：只支持 PyInstaller 单文件 exe 的发布方案。
2. [02-window-layout-and-topmost.md](02-window-layout-and-topmost.md)：窗口就绪、布局、置顶和焦点策略。
3. [03-screenshot-and-dpi.md](03-screenshot-and-dpi.md)：WGC 截图、物理像素和 DPI 约定。
4. [04-user-list-and-activation.md](04-user-list-and-activation.md)：用户列表识别、右上角 OCR 验证和激活点击。
5. [05-ocr-compose-pipeline.md](05-ocr-compose-pipeline.md)：拼图 OCR、缩放还原和文本切分。
6. [06-message-read-and-retry.md](06-message-read-and-retry.md)：消息读取、激活与失败重试。
7. [07-send-and-command-ledger.md](07-send-and-command-ledger.md)：发送流程、幂等键和结果状态。
8. [08-http-api-and-contracts.md](08-http-api-and-contracts.md)：FastAPI 接口、中文 OpenAPI 和错误语义。
9. [09-chat-history.md](09-chat-history.md)：按用户列表名字维护的聊天记录。
10. [10-logging-and-diagnostics.md](10-logging-and-diagnostics.md)：应用日志、控制台捕获和诊断字段。
11. [11-testing-and-operations.md](11-testing-and-operations.md)：构建、测试、冒烟和运行检查清单。

## 统一约定

- 发行形态：只支持 PyInstaller 单文件 exe。
- 运行形态：Windows 登录用户交互桌面，不注册 Windows Service。
- 网络边界：只监听 `127.0.0.1` 随机端口。
- 联系人键：使用 QQ 用户列表中的名字。
- 串行边界：所有 QQ 桌面操作通过 `Dispatcher` 单消费者队列执行。
- 日志：应用结构化日志和完整控制台日志都写入 `<state-dir>/logs/`。