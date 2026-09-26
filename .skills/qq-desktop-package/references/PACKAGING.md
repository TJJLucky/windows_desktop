# 项目打包细节（price-agent-windows-desktop）

## 唯一发布产物：单文件 exe

| 工具 | 产物 | 脚本 |
| --- | --- | --- |
| PyInstaller onefile | `dist-exe/qq-desktop-service.exe`（~110MB） | `scripts/build_exe.py` |

wheel 方案已废弃（`build_and_verify.py` 已删除，pyproject 移除 build-system 与 hatch 配置），
不再产出 `dist/`。打包/发布只走 exe。

## 为什么需要 entry.py

`service/__main__.py` 内部用包内相对导入（`from .app import ...`）。PyInstaller 直接以它作入口时，
模块被当作顶层 `__main__` 执行，相对导入报 `attempted relative import with no known parent package`。
项目根 `entry.py` 以 `from service.__main__ import main` 包方式加载，保留包上下文。

## PyInstaller 参数（build_exe.py 内置）

| 参数 | 作用 |
| --- | --- |
| `--onefile --console` | 单文件控制台程序 |
| `--add-data templates;templates` | 模板图 → `_MEIPASS/templates`（代码相对路径恰好指向） |
| `--add-data libs/wgc_capture.dll;libs` | WGC DLL → `_MEIPASS/libs` |
| `--collect-data rapidocr_onnxruntime` | OCR 模型与配置（缺则运行时 OCR 失败） |
| `--hidden-import pyautogui/pyscreeze/mouseinfo/pywinauto/psutil/pygetwindow` | 隐式导入兜底 |
| `--distpath dist-exe --workpath build-exe --specpath build-exe` | 产物/工作目录隔离 |

## exe 运行时的资源路径

onefile 运行时 `__file__` 指向 `_MEIPASS/utils/qq/xxx.py`，代码 `parent.parent.parent` 上溯到 `_MEIPASS`，
因此 `_TEMPLATE_DIR`/DLL 路径天然命中 add-data 目标，无需改代码。

## 已踩的坑（勿重犯）

1. `--add-data` 相对路径按 **spec 目录**解析 → 必须传绝对路径。
2. exe 常驻（uvicorn 服务）会持有父进程输出管道句柄 → 父进程等 EOF 永久卡。
   冒烟脚本中所有 exe 子进程一律重定向 stdout/stderr（DEVNULL）。
3. PyInstaller onefile 是**父子进程模型**（bootloader pid ≠ 服务 processId）。
   terminate 只杀 bootloader，需 `taskkill /PID <bootloader> /T /F` 连树清理。
4. 构建产物 111MB 属正常（onnxruntime + opencv + numpy）；校验上限 400MB 防失控。

## 依赖清单（pyproject dependencies）

fastapi / pydantic / uvicorn / httpx / numpy / opencv-python / Pillow / pyautogui / **psutil** / pyperclip / pywin32(win32) / pywinauto / rapidocr-onnxruntime。
注意：`psutil` 是 `window_ops.py` 的运行时依赖，曾漏声明，已补。

pyproject.toml 仅保留项目元数据与 pytest/ruff 配置（无 build-system，wheel 不可构建，符合"唯一 exe 方案"）。

## conda 环境

专用环境 `qq-desktop-service`（D:\anaconda3\envs\qq-desktop-service，Python 3.12，含 PyInstaller）。
构建/运行统一用它，勿混用其他环境。

## 运行

**默认模式（最终用户，双击即用）**：不带任何参数，数据目录自动落到 `%LOCALAPPDATA%\price-agent-qq-service\`：

```powershell
dist-exe\qq-desktop-service.exe
# 或源码：python -m service
```

已在运行时再次启动会提示并退出（防重复拉起）。

**显式模式（开发者/集成方）**：

```powershell
conda activate qq-desktop-service
price-agent-qq-service --endpoint-file <endpoint.json> --state-dir <state-dir>
# 或 python -m service --endpoint-file ... --state-dir ...
# 或 exe：dist-exe\qq-desktop-service.exe --endpoint-file ... --state-dir ...
```

服务绑定 127.0.0.1 随机端口，把 endpoint+token 写入 endpoint 文件。
**调试**：浏览器打开 `http://127.0.0.1:<port>/docs`（Swagger UI，Authorize 填 token 后可直接发请求）；OpenAPI 合同 `GET /openapi.json`。

## 产物验证（冒烟）

```powershell
# --help 必须含 endpoint-file/state-dir；--smoke-start 时启动后 /v1/health == READY
python scripts\build_exe.py --project-dir E:\Project\agent\windows_desktop --smoke-start
# 只校验现有 exe（不重新构建）
python scripts\build_exe.py --project-dir E:\Project\agent\windows_desktop --skip-build
```

## 校验清单变更规则

每次改 PyInstaller 参数（COLLECT_DATA / ADD_DATA / HIDDEN_IMPORTS）或产物路径后，同步更新
`scripts/build_exe.py` 里的常量与 `verify_exe` 的断言，并跑一次 `--skip-build` 验证。
