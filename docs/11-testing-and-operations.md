# 真机验收与运行检查

本项目不再维护单元测试，也不以 mock/离线用例作为交付依据。QQ 桌面自动化必须在真实的 Windows 交互桌面、真实 QQ 窗口上验收。

## 环境

统一使用 Conda：

```powershell
# 运行时/打包环境
conda run -n qq-desktop-service python ...
```

不要用 `.venv`、MSYS Python 或 uv 代替约定的 Conda 环境。

## exe 冒烟（先过这一关）

```powershell
conda run -n qq-desktop-service python .skills\qq-desktop-package\scripts\build_exe.py --project-dir E:\Project\agent\windows_desktop --smoke-start
# 只校验现有 exe（不重新构建）
conda run -n qq-desktop-service python .skills\qq-desktop-package\scripts\build_exe.py --project-dir E:\Project\agent\windows_desktop --skip-build
```

验证：

- `--help` 包含启动参数。
- `/v1/health` 返回 `READY`。
- 默认模式 endpoint 正常生成。
- 日志目录和日志文件正常生成。

冒烟只证明进程能起来，不证明 QQ 操作可用。QQ 相关能力必须继续做下面的真机验收。

## 真机验收前置条件

- Windows 交互桌面（不能是锁屏、远程断开或无人值守会话）。
- QQ 已登录，主窗口可见、未最小化，联系人列表可滚动。
- 服务同一时间只运行一个实例；端口来自 endpoint 文件，不写死。
- 知道本轮要用的目标会话名（`/v1/contacts:query` 返回的 key）和用于回读的唯一标记。
- 真机操作默认只发给自己或已确认的测试联系人；禁止对真实供应商批量发送。

## 真机验收步骤

### 1. 启动并读健康状态

用显式模式启动，便于保留本轮 endpoint、state、log：

```powershell
conda run -n qq-desktop-service python -m service --endpoint-file <endpoint.json> --state-dir <state-dir> --log-dir <log-dir>
```

读取 endpoint 文件里的 `port`、`processId` 和日志路径，确认进程与文件一致，再检查：

- `GET /v1/health` 返回 `READY`。
- 日志文件开始写入。
- `processId` 对应的是当前服务进程，不是残留进程。

### 2. 每次操作前重新查联系人

不要缓存上一次的联系人结果。真机每次发送前先：

- `GET /v1/contacts:query` 取当前会话名单。
- 确认目标会话名在结果里，且刷新时间足够新。

### 3. 会话定位与读取验收

- 会话定位走无障碍树：`/v1/contacts:query` 传 `{"contactName":"..."}` 时先读当前屏，未命中才滚动查找；命中即按行名精确匹配，不做模糊匹配。
- 空白会话（没有任何消息）必须返回 0 条消息、不进入框选、不报错、不卡死；真机至少手动确认一个空白会话（如 `！`）返回 `n=0`。
- QQ 拒绝复制的会话（`我的手机`、`QQ邮箱提醒`、`QQ团队`、临时会话邀请卡片等）应被名单筛掉；直接读取它们必须毫秒级返回 `QQ_CONTACT_NOT_COPYABLE`，不再跑十几秒的失败框选。
- `POST /v1/contacts/current:identify`（号码识别）保留为诊断用：只识别当前激活行，不发送消息；菜单没有“复制QQ号”时返回跳过原因（如 `QQ_CONTACT_NUMBER_COPY_UNAVAILABLE`）。

### 4. 发送与回读验收

- `POST /v1/commands/send` 发送前再次确认目标会话名。
- 轮询 `GET /v1/commands/{commandId}`，只把 `SUCCEEDED` 视为命令完成；`pending_outbound` 不等于已送达。
- 用 `GET /v1/commands/read` 或目标会话人工回读唯一标记，确认消息真实出现在正确会话里。
- 发送失败、窗口变化或采集超时都按失败处理，不自动重放点击。

### 5. 客户端联动检查

- 客户端使用 `apps/desktop/resources/qq/qq-desktop-service.exe`；改动 QQ 服务后先 `npm run prepare:qq` 同步。
- 客户端显示已连接时，才能进行端到端 UI 验收。
- 客户端连不上和服务本身可用是两件事；连不上时先看 endpoint 文件、进程、健康接口和客户端连接日志，不要直接判定 QQ 能力已通过。

### 6. 收尾

- 停止本轮临时启动的服务进程。
- 确认 endpoint/端口文件已清理，没有残留子进程占用 DLL 或端口。
- 记录本轮证据：endpoint、日志、命令 ID、返回状态、回读结果、失败原因。

## 验收记录模板

| 项目 | 期望 | 实际 | 结论 |
| --- | --- | --- | --- |
| `/v1/health` | `READY` | | |
| 联系人查询 | 目标会话名在结果中 | | |
| 空白会话 | 返回 0 条，不框选不报错 | | |
| 不可复制会话 | 名单筛掉，直接读取毫秒级 `QQ_CONTACT_NOT_COPYABLE` | | |
| 发送命令 | 最终 `SUCCEEDED` | | |
| 会话回读 | 唯一标记出现在正确会话 | | |
| 客户端连接 | 显示已连接 | | |
| 收尾 | 无残留进程与端口文件 | | |

## 运行注意事项

- 必须运行在 Windows 交互桌面。
- 交付只复制 `dist-exe/qq-desktop-service.exe`；客户端使用前先同步资源。
- 真实 QQ 验证重点检查：窗口置顶、无障碍树会话定位与滚动查找、激活确认、空白会话判空、不可复制会话过滤、框选复制与剪贴板读取。
- 任何“通过”结论都必须来自真机证据；没有真机证据只能写“未验证”。
