# 测试与运行检查

## 环境

开发和测试统一使用 Conda：

```powershell
conda run -n chip-price-consult python -m pytest -q
```

exe 构建使用：

```powershell
conda run -n qq-desktop-service python ...
```

## 自动测试

重点覆盖：

- HTTP 契约和状态码。
- 中文 OpenAPI 描述。
- 命令幂等。
- 聊天记录窗口差异。
- 拼图 OCR y 坐标和 scale 还原。
- 用户激活坐标映射。
- 日志文件创建和控制台捕获。

不依赖真实 QQ 的测试不能证明桌面视觉操作已经可用；真实操作仍要做人工或端到端验证。

## exe 冒烟

```powershell
python .skills\qq-desktop-package\scripts\build_exe.py --project-dir E:\Project\agent\windows_desktop --smoke-start
```

验证：

- `--help` 包含启动参数。
- `/v1/health` 返回 `READY`。
- 默认模式 endpoint 正常生成。
- 日志目录和日志文件正常生成。

## 运行注意事项

- 必须运行在 Windows 交互桌面。
- 不要用 `.venv`、MSYS Python 或 uv 代替约定的 Conda 环境。
- 交付只复制 `dist-exe/qq-desktop-service.exe`。
- 真实 QQ 验证重点检查：窗口置顶、列表滚动、右上角 OCR、点击坐标和消息读取重试。