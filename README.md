# Windows 桌面集成（windows_desktop）

对 Windows 桌面 GUI（以 QQ 为主）进行自动化操作的集成包：窗口管理、后台截图（WGC）、图像识别（模板匹配 + OCR）、无障碍树（UI Automation）读取、鼠标键盘模拟，以及 QQ 会话列表/输入框/消息区的识别与操作。

`utils/` 是实现层；`service/` 是对外接口层。所有 QQ 操作统一经由 `Dispatcher` 单消费者队列执行，保证全局不并发。

发行形态仅支持 PyInstaller 生成的 exe；不提供 wheel、pip console script 或其他 Python 安装形态。源码运行仅用于开发和真机验收。

---

## 目录结构

```text
windows_desktop/
├── __init__.py          # 包入口说明
├── pyproject.toml        # 依赖、代码风格与 exe 构建元数据
├── libs/wgc_capture.dll  # WGC 原生截获库
├── templates/*.png       # QQ 界面模板图（区域定位依赖）
├── service/              # 本机 HTTP API、命令账本、服务入口
├── debug/                # 调试输出图片
├── .skills/qq-desktop-package/   # 打包 skill（exe 构建脚本 build_exe.py）
└── utils/
    ├── __init__.py        # 顶层重导出 + 启动时 ensure_dpi_aware()
    ├── core/             # 平台原子原语：截图/鼠标/窗口
    ├── vision/           # 图像分析：模板匹配/OCR
    └── qq/               # QQ 业务组合：用户/输入框/消息/单消费者队列
```

---

## 分层与依赖

单向依赖，无循环引用：

```text
core  →  vision  →  qq
```

外部调用路径固定为：

```text
HTTP Client -> service/ -> Dispatcher -> utils/qq -> utils/vision + utils/core
```

调用方不得越过 `service/` 直接 import `utils/`。这样 WGC、OCR、模板与窗口操作可以继续演进，而接口契约保持稳定。

| 层 | 职责 | 依赖 |
| --- | --- | --- |
| `utils/core` | 平台原子原语（截图、鼠标、窗口） | 无 |
| `utils/vision` | 图像分析（模板匹配、OCR） | core |
| `utils/qq` | QQ 业务组合（区域识别、联系人、单消费者队列） | core + vision |

---

## 核心约定

### DPI 与物理像素

- `utils/__init__.py` 加载时调用 `screenshot.ensure_dpi_aware()`（per-monitor V2），统一为物理像素坐标。
- `GetWindowRect` / WGC / `SetCursorPos` 均为物理像素；图片坐标转屏幕坐标时 **不再除以缩放系数**。
- `getDPI()` 返回系统缩放比（如 144 → 1.5），仅用于计算/校验。

### 截图：WGC

- 窗口像素只允许来自 `wgc` 对明确 HWND 的原生后台截获（`core/screenshot.py`），不使用前台屏幕截图。
- 唤起 QQ 窗口使用全局快捷键（Ctrl+Alt+X：打开/隐藏所有窗口），无托盘截图依赖。

### 单消费者

- `Dispatcher` 是全局唯一的 QQ 消费入口（单例 + daemon worker + 队列）。
- skill 工具全部入队串行，确保同一时刻只有一个操作在控制 QQ。
- 阻塞 API 统一返回 `(result, exc)`；超时仅表示未等到结果，不保证任务未执行。

---

## 独立 QQ Service

服务运行于当前 Windows 登录用户的交互桌面会话，不注册为 Windows Service，也不开放公网端口。它只监听由系统分配的 `127.0.0.1:0` 动态端口，启动后将 endpoint（loopback URL + pid）原子写入 endpoint 文件。服务**不做 token 鉴权**：安全边界是"仅监听本机 loopback"，调用方直接请求即可。

**默认模式（最终用户，双击即用）**：发行版只提供 exe；不带任何参数双击运行，数据目录自动落到 `%LOCALAPPDATA%\price-agent-qq-service\`：

```powershell
# 发行版（唯一支持的交付方式）
qq-desktop-service.exe
```

源码调试可在项目根执行 `python -m service`，该方式不属于交付/安装承诺。

**显式模式（源码调试/集成排查）**：指定 endpoint 文件与状态目录：

```powershell
conda run -n qq-desktop-service python -m service `
  --endpoint-file C:\PriceAgent\state\qq-service-endpoint.json `
  --state-dir C:\PriceAgent\state\qq-service
```

默认模式下若服务已在运行（endpoint 文件存在且进程存活），再次启动会提示并退出，防止双击重复拉起。启动日志会打印 endpoint 文件路径与调试页面地址。

endpoint 文件记录动态 loopback URL 与本次启动的进程 pid（无 token 字段——服务不做鉴权）。调用方读取 endpoint 后直接调用接口，无需任何鉴权头。

日志默认写入 `<state-dir>\logs\`，也可用 `--log-dir` 指定目录：

- `qq-desktop-service.log`：应用结构化日志，含时间、级别、模块、请求 ID、请求耗时、状态码和关键业务字段。
- `qq-desktop-service.console.log`：完整捕获控制台输出，包括 Uvicorn 和 `print()` 输出。
- 单个日志文件达到 5 MB 后轮转，保留 5 个历史文件。
- endpoint 文件同时写入 `logDirectory`、`applicationLog`、`consoleLog`，调用方可直接定位。

聊天记录以 `contactName` 为分区键；调用方应使用 `/v1/contacts:query` 返回的会话名单 key 原样传回。

接口：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/v1/health` | 服务和 API 版本状态 |
| `POST` | `/v1/contacts:query` | 读取 QQ 会话名单（`{"all":true}` 滚动扫掠整张名单） |
| `POST` | `/v1/contacts/current:identify` | 复制当前激活行的 QQ 号（诊断用，不发送） |
| `POST` | `/v1/commands/read` | 读取指定联系人的可见消息 |
| `POST` | `/v1/commands/send` | 向指定联系人发送消息 |
| `GET` | `/v1/commands/{commandId}` | 查询发送命令状态 |
| `POST` | `/v1/chat/history` | 查询聊天记录（查询前自动视觉更新一次） |
| `POST` | `/v1/capture:check` | 检查 QQ 截图是否可用（窗口就绪探测，200 + ready 标记） |

`send` 必须提供调用方生成的稳定 `commandId`。相同 ID 与相同消息体只执行一次；相同 ID 配不同消息体返回 `409`。服务重启发现尚未确认的 `RUNNING` 命令时统一转为 `EFFECT_UNKNOWN`，不自动重发。

**调试**：浏览器打开启动日志中的 `http://127.0.0.1:<port>/docs`（FastAPI Swagger UI），无需登录/填 token，直接选接口、填参数、发请求；机器可读合同见 `GET /openapi.json`。

接口示例：

```http
POST /v1/commands/send
Content-Type: application/json

{
  "commandId": "cloud-effect-key-or-caller-uuid",
  "contactName": "华强电子",
  "text": "STM32F103C8T6，数量 100，请报价。"
}
```

```json
{
  "commandId": "cloud-effect-key-or-caller-uuid",
  "status": "SUCCEEDED",
  "result": {"ok": true, "sent": true, "textLength": 26, "error": null}
}
```

---

## 模块说明

### `utils/core`

| 文件 | 内容 |
| --- | --- |
| `screenshot.py` | `ensure_dpi_aware()` / `getDPI()` / `calc_buf_size()` / `WGCCapture`（后台截图单例） |
| `mouse.py` | `random_point` / `random_click` / `click_at` / `drag`（`SetCursorPos` + `mouse_event`） |
| `window.py` | `WindowCaptureCtx`（无焦点置顶上浮）、`set_window_z_pos`、`layout_window_left_half`（高度铺满+宽50%+靠左，取代最大化）、贴边检测 |
| `timing.py` | `timer` 装饰器（函数耗时输出） |

### `utils/vision`

| 文件 | 内容 |
| --- | --- |
| `matcher.py` | `find_template`（TM_CCOEFF_NORMED） / `draw_box` / `crop_image` / `crop_region` |
| `ocr.py` | `OCREngine` 单例（RapidOCR + ONNX Runtime）；模型路径三级：`RAPIDOCR_MODEL_PATH` → `<install>/ocr-models` → 包内 models |

### `utils/qq`

| 文件 | 内容 |
| --- | --- |
| `window_ops.py` | QQ 进程/窗口枚举、快捷键唤起（Ctrl+Alt+X 打开/隐藏所有窗口，有进程无窗口时唤醒）、`get_main_window` / `ensure_qq_window_with_retry` |
| `regions.py` | `RegionResult`；`get_userList_region_and_image` / `get_inputbox_region_and_image` / `get_message_box_region_and_image` / `get_input_buttom_region`；模板在 `templates/` |
| `models.py` | 数据模型：`User`（`to_dict`） |
| `user_list.py` | `UserList`（单例）：`activate`（按会话名激活，见 `contact_lookup`）、`get_user_list`（会话名单；`all_contacts=True` 扫掠整张名单）、`refresh_image`（截图与区域，供红点/激活态图像判定） |
| `input.py` | `InputBox`（单例）：`send_text(contact_name, text)` 内部按会话名激活（无障碍树）→聚焦→剪贴板粘贴→Ctrl+V→按 Enter 快捷键发送 |
| `message.py` | `MessageList`（单例）：`read_messages(contact_name)` 内部按会话名激活→判空（空白会话直接返回 0 条）→左上到右下框选→定位并点击 `copy_icon.png`→等剪贴板写入后退出多选→解析 sender/timestamp/text/rawText |
| `hotkeys.py` | QQ 全局快捷键配置表（Ctrl+Alt+X 打开/隐藏所有窗口、Enter 发送）与 `send_hotkey` 发送原语 |
| `dispatcher.py` | `Dispatcher` 单消费者任务队列（唯一入口）：`get_contact_list` / `send_message` / `read_message_list`（发送/读取内部自动激活用户） |

---

## 使用示例

正式流程统一通过 `Dispatcher` 入队（QQ 需已登录，窗口会自动置前）：

```python
from utils.qq.dispatcher import Dispatcher

op = Dispatcher()

# payload = {"contacts": 会话名 → 行信息, "complete": 是否覆盖整张名单}
# all_contacts=True 时滚动扫掠整张名单（不只当前一屏），更慢但更全
payload, exc = op.get_contact_list(timeout=60.0)
if exc is not None:
    raise exc

ok, exc = op.send_message("华强电子", "STM32F103 什么价格？", timeout=120.0)

messages, exc = op.read_message_list("华强电子", timeout=120.0)
```

对应 skill 工具（`skills/windows_desktop/main/`）：

| 工具 | 队列 API |
| --- | --- |
| `list_qq_contacts` | `get_contact_list()` |
| `send_qq_message(contact_name, text)` | `send_message(contact_name, text)` |
| `read_qq_messages(contact_name)` | `read_message_list(contact_name)` |

---

## 注意事项

- **非 Windows 平台**：不支持；exe 依赖 Windows 交互桌面会话。
- **templates/**：QQ 界面区域定位的模板图，不可删除；QQ 界面改版后需更新。
- **OCR 模型**：exe 打包后模型放 `<install>/ocr-models`（3 个 onnx），或设置 `RAPIDOCR_MODEL_PATH` 环境变量；包内 models 打包时会被清空。
- **单例直调**：`UserList`/`InputBox`/`MessageList` 可在本地调试时直接使用，但正式流程必须经 `Dispatcher`，避免并发操作 QQ。
- **补充资源**：`libs/wgc_capture.dll` 原生截获库；`debug/` 调试图片可随时清理。


### 会话定位与名单（无障碍树）

会话身份取 QQ 会话列表里会话行自报的名字（好友昵称/备注、群名、官方号名），统一叫
`contactName`；不再使用 QQ 号作为主键，也不做昵称 OCR。查找、滚动、激活都走 UI Automation
无障碍树（`utils/qq/contact_lookup.py` + `utils/qq/accessibility.py`）：

- `POST /v1/contacts:query` 可传 `{"contactName":"华强电子"}`：先读当前屏，未命中才在列表区滚动查找（优先 UIA `ScrollPattern` 按百分比定位，容器不发布该 Pattern 时退回滚轮逐段下滚）。
- 要一次拿到**全部**会话用 `{"all": true}`：滚动扫掠每一段再按昵称合并，响应里的 `complete` 标注是否确认扫全（`false` = 没扫全，不要当全量用）。
- 行自带屏幕物理像素矩形，命中行的中心点即点击坐标；已是当前会话时直接返回、不点击（点击已激活行会切掉激活高亮）。
- 点击后在 4 秒窗口内轮询两个独立信号确认切换生效（聊天区标题 == 目标名，或目标行带上激活标记），两个信号每轮都判；同名多行报 `QQ_CONTACT_AMBIGUOUS`。
- 名单快照默认筛掉 QQ 没给复制项的会话（`我的手机`、`QQ邮箱提醒` 等，判据见行结构），以及本机读过的“QQ 拒绝复制”会话（`QQ团队`、临时会话邀请卡片、只含系统占位消息的会话）。
- **空会话**：没有任何消息的会话读取时返回 0 条消息，不进入框选、不报错（判空细节见 [docs/06](docs/06-message-read-and-retry.md)）。

### 消息读取

消息正文仍走 QQ 原生框选复制（不做 OCR）：按会话名激活 → 判空 → 左上到右下框选 → 定位并点击复制按钮 → 等剪贴板写入后按 Esc 退出多选 → 解析 `sender`/`timestamp`/`text`/`rawText`。
空白会话在框选前就返回 0 条。判空视口、提示浮层、异步复制与失败语义见 [docs/06-message-read-and-retry.md](docs/06-message-read-and-retry.md)。

### 不可复制会话（学习记录）

QQ 对 `我的手机`、`QQ邮箱提醒`、官方号 `QQ团队`、临时会话邀请卡片、只含系统占位消息的会话等
禁用了框选复制。首次真实读取失败后按昵称记进 `<state>/non-copyable-contacts.json`，之后从
名单筛掉；直接读取它们毫秒级返回 `QQ_CONTACT_NOT_COPYABLE`（HTTP 503），不再重跑十几秒的
失败框选。

- 分强弱两级：进了多选态才失败的码（剪贴板不变/文本为空/复制项缺失）一次即生效；没进多选态
  （`QQ_SELECTION_NOT_ENTERED`）是弱证据，**连续两次**才生效——真机上没有消息的空会话也会报这个码，
  只记一次就把真实供应商静默筛掉了。
- 关闭：环境变量 `QQ_CONTACT_REFUSAL_LEARNING=0`。
- 清空：删除该 JSON 文件。

### 号码识别（诊断）

`POST /v1/contacts/current:identify` 只识别当前激活行、不发送：头像定位当前行 → 右键 → 匹配
`copy_qq_number` 模板 → 点击 → 校验剪贴板序列、进程归属与纯号码。成功返回
`{"qqNumber":"800888598"}`；菜单没有“复制 QQ 号”时返回 HTTP 200
`{"qqNumber":null,"skipped":true,"reason":"QQ_CONTACT_NUMBER_COPY_UNAVAILABLE"}`。此接口不再
充当联系人查找手段，只留作诊断；窗口/标题/行身份变化、菜单改变或歧义、剪贴板超时与非号码均
返回明确 `QQ_CONTACT_NUMBER_*` 错误（HTTP 503），不自动重试、不回退昵称 OCR。
