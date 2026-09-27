# exe-only 打包方案

## 目标

项目只支持 PyInstaller 单文件 exe，不提供 wheel、pip console script 或其他 Python 安装形态。源码运行只用于开发、调试和集成排查。

## 构建产物

构建脚本：

```text
.skills/qq-desktop-package/scripts/build_exe.py
```

默认产物：

```text
dist-exe/qq-desktop-service.exe
```

PyInstaller 中间目录：

```text
build-exe/
```

`build-exe/` 包含 `.spec`、TOC、PYZ、临时 exe 等构建中间文件，不是第二个发行目录。交付时只需要 `dist-exe/`。

## PyInstaller 关键参数

- `--onefile`：单文件 exe。
- `--console`：保留控制台，便于查看和捕获日志。
- `--distpath dist-exe`：最终产物目录。
- `--workpath build-exe`：构建工作目录。
- `--specpath build-exe`：spec 文件目录。
- `--add-data templates;templates`：打包 QQ 区域模板。
- `--add-data libs/wgc_capture.dll;libs`：打包 WGC 原生库。
- `--collect-data rapidocr_onnxruntime`：收集 OCR 模型和配置。
- `--hidden-import pyautogui/pyscreeze/mouseinfo/pywinauto/psutil/pygetwindow`：补充隐式依赖。

## 构建与冒烟

```powershell
conda run -n qq-desktop-service python `
  .skills\qq-desktop-package\scripts\build_exe.py `
  --project-dir E:\Project\agent\windows_desktop `
  --smoke-start
```

脚本会验证：

- exe 体积不超过 400 MB。
- `--help` 包含 `--endpoint-file`、`--state-dir`、`--log-dir`。
- 临时启动后 `/v1/health` 返回 `READY`。
- 默认模式能够创建 endpoint 文件并健康响应。

只校验已有 exe：

```powershell
conda run -n qq-desktop-service python `
  .skills\qq-desktop-package\scripts\build_exe.py `
  --project-dir E:\Project\agent\windows_desktop `
  --skip-build
```

## 不支持的路径

- 不执行 `pip install .` 作为交付方式。
- 不支持 wheel。
- 不依赖 `price-agent-qq-service` console script。
- 不把 `build-exe/` 作为交付目录。