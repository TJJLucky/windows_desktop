# 日志与诊断

## 日志目录

默认：

```text
%LOCALAPPDATA%\price-agent-qq-service\state\logs\
```

显式模式：

```text
<state-dir>\logs\
```

也可以用 `--log-dir` 覆盖。

## 文件

- `qq-desktop-service.log`：结构化应用日志。
- `qq-desktop-service.console.log`：控制台和 Uvicorn 输出捕获。

两个文件都按 5 MB 轮转，保留 5 份历史。

## 结构化字段

应用日志包含：

- 时间、毫秒、级别、logger 名称。
- Python、平台、PID、工作目录。
- endpoint、state、application log、console log 路径。
- 请求 ID、HTTP 方法、路径、状态码、耗时。
- 发送命令 ID、联系人、文本长度、状态。
- 聊天记录新增数量和异常堆栈。

## 控制台捕获

`TeeStream` 同时写原始控制台和日志文件，因此 Uvicorn 日志和 `print()` 都会进入 console log。Uvicorn 使用 `use_colors=False`，不写入 ANSI 颜色控制码。

## endpoint 诊断

endpoint 文件的 `logDirectory`、`applicationLog`、`consoleLog` 字段可直接定位日志，不需要猜测路径。