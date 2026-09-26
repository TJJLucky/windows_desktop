# Windows 桌面集成（windows_desktop）

对 Windows 桌面 GUI（以 QQ 为主）进行自动化操作的集成包：窗口管理、后台截图（WGC）、图像识别（模板匹配 + OCR）、鼠标键盘模拟，以及 QQ 用户列表/输入框/消息区的识别与操作。

`utils/` 是实现层；`service/` 是对外接口层。所有 QQ 操作统一经由 `Dispatcher` 单消费者队列执行，保证全局不并发。

---

## 目录结构

```text
windows_desktop/
├── __init__.py          # 包入口说明
├── pyproject.toml        # 独立发布与服务命令入口
├── libs/wgc_capture.dll  # WGC 原生截获库
├── templates/*.png       # QQ 界面模板图（区域定位依赖）
├── service/              # 本机 HTTP API、认证、命令账本、服务入口
├── debug/                # 调试输出图片
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
- 受保护窗口（如托盘）使用 `ImageGrab` 辅助。

### 单消费者

- `Dispatcher` 是全局唯一的 QQ 消费入口（单例 + daemon worker + 队列）。
- skill 工具全部入队串行，确保同一时刻只有一个操作在控制 QQ。
- 阻塞 API 统一返回 `(result, exc)`；超时仅表示未等到结果，不保证任务未执行。

---

## 独立 QQ Service

服务运行于当前 Windows 登录用户的交互桌面会话，不注册为 Windows Service，也不开放公网端口。它只监听由系统分配的 `127.0.0.1:0` 动态端口，启动后将 endpoint 和一次性 token 原子写入 endpoint 文件。

**默认模式（最终用户，双击即用）**：不带任何参数运行（exe 双击 / `python -m service`），数据目录自动落到 `%LOCALAPPDATA%\price-agent-qq-service\`：

```powershell
# exe
qq-desktop-service.exe
# 源码/环境
python -m service
```

**显式模式（开发者/集成方）**：指定 endpoint 文件与状态目录：

```powershell
conda run -n qq-desktop-service python -m service `
  --endpoint-file C:\PriceAgent\state\qq-service-endpoint.json `
  --state-dir C:\PriceAgent\state\qq-service
```

默认模式下若服务已在运行（endpoint 文件存在且进程存活），再次启动会提示并退出，防止双击重复拉起。启动日志会打印 endpoint 文件路径与调试页面地址。

endpoint 文件包括动态 loopback URL 和本次启动 token。它必须保存到仅当前用户可读的目录；调用方读取后使用 `Authorization: Bearer <token>` 调用接口。

接口：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/v1/health` | 服务和 API 版本状态 |
| `POST` | `/v1/contacts:query` | 读取当前 QQ 联系人 |
| `POST` | `/v1/commands/read` | 读取指定联系人的可见消息 |
| `POST` | `/v1/commands/send` | 向指定联系人发送消息 |
| `GET` | `/v1/commands/{commandId}` | 查询发送命令状态 |
| `POST` | `/v1/chat/history` | 查询聊天记录（查询前自动视觉更新一次） |

`send` 必须提供调用方生成的稳定 `commandId`。相同 ID 与相同消息体只执行一次；相同 ID 配不同消息体返回 `409`。服务重启发现尚未确认的 `RUNNING` 命令时统一转为 `EFFECT_UNKNOWN`，不自动重发。

**调试**：浏览器打开启动日志中的 `http://127.0.0.1:<port>/docs`（FastAPI Swagger UI），右上角 Authorize 填入 endpoint 文件里的 token 后即可直接选接口、填参数、发请求；机器可读合同见 `GET /openapi.json`。

接口示例：

```http
POST /v1/commands/send
Authorization: Bearer <endpoint-token>
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
| `window.py` | `WindowCaptureCtx`（无焦点置顶上浮）、`set_window_z_pos`、窗口移动/最大化/贴边检测 |
| `timing.py` | `timer` 装饰器（函数耗时输出） |

### `utils/vision`

| 文件 | 内容 |
| --- | --- |
| `matcher.py` | `find_template`（TM_CCOEFF_NORMED） / `draw_box` / `crop_image` / `crop_region` |
| `ocr.py` | `OCREngine` 单例（RapidOCR + ONNX Runtime）；模型路径三级：`RAPIDOCR_MODEL_PATH` → `<install>/ocr-models` → 包内 models |
| `compose.py` | 视觉编排占位（气泡拼图 OCR、列表归一化等性能优化后续实现） |

### `utils/qq`

| 文件 | 内容 |
| --- | --- |
| `window_ops.py` | QQ 进程/窗口枚举、托盘唤起（非视觉优先 + 视觉兜底）、`get_main_window` / `ensure_qq_window_with_retry` |
| `regions.py` | `RegionResult`；`get_userList_region_and_image` / `get_inputbox_region_and_image` / `get_message_box_region_and_image` / `get_input_buttom_region`；模板在 `templates/` |
| `models.py` | 数据模型：`User`（`to_dict`）与 `Message`（text/rect/is_self） |
| `user_list.py` | `UserList`（单例）：`find_user`（包含匹配 + 中英文规范化）、`active_user_by_name`、`get_user_list`（1s 缓存） |
| `input.py` | `InputBox`（单例）：聚焦→剪贴板粘贴→Ctrl+V→点击发送按钮中心 |
| `message.py` | `Message` + `MessageList`（单例）：气泡检测→OCR→判断发送方 `is_self` |
| `dispatcher.py` | `Dispatcher` 单消费者任务队列（唯一入口）：`get_contact_list` / `send_message` / `read_message_list` |

---

## 使用示例

正式流程统一通过 `Dispatcher` 入队（QQ 需已登录，窗口会自动置前）：

```python
from utils.qq.dispatcher import Dispatcher

op = Dispatcher()

contacts, exc = op.get_contact_list(timeout=60.0)
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

- **非 Windows 平台**：包可以 import 成功，但实际调用会失败。
- **templates/**：QQ 界面区域定位的模板图，不可删除；QQ 界面改版后需更新。
- **OCR 模型**：打包后模型放 `<install>/ocr-models`（3 个 onnx），或设置 `RAPIDOCR_MODEL_PATH` 环境变量；包内 models 打包时会被清空。
- **单例直调**：`UserList`/`InputBox`/`MessageList` 可在本地调试时直接使用，但正式流程必须经 `Dispatcher`，避免并发操作 QQ。
- **补充资源**：`libs/wgc_capture.dll` 原生截获库；`debug/` 调试图片可随时清理。
