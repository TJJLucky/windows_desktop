---
name: qq-desktop-package
description: 构建并校验 price-agent-windows-desktop（QQ 桌面视觉 RPA 服务）的唯一发布产物：单文件 exe（PyInstaller onefile）。当用户要求"打包成 exe"、"构建可执行文件"、"打包这个项目"、"准备发布/分发"、"验证打包产物"、"看看产物里有什么/缺什么"或涉及 dist-exe/ 产物检查时使用。覆盖：PyInstaller 资源收集（templates/libs/OCR 模型）、包外入口处理、exe 启动冒烟（--help + /v1/health）、产物体积上限校验。
---

# QQ Desktop 打包（唯一方案：单文件 exe）

## 何时用

用户对 `price-agent-windows-desktop` 项目提出打包/发布/产物检查类需求时使用本 Skill。
项目路径：`E:\Project\agent\windows_desktop`。

## 快速开始

```powershell
# 构建单文件 exe + --help 冒烟 + 启动冒烟（默认 qq-desktop-service 环境）
python scripts\build_exe.py --project-dir E:\Project\agent\windows_desktop --smoke-start

# 只校验现有 exe / 指定解释器 / 指定产物名
python scripts\build_exe.py --project-dir <dir> --skip-build
python scripts\build_exe.py --project-dir <dir> --python D:\anaconda3\envs\<env>\python.exe --name qq-desktop-service
```

退出码 0 = 可交付；非 0 = 有 `[FAIL]` 行，按提示修复后重跑。

产物：`dist-exe\qq-desktop-service.exe`（约 110MB，含 onnxruntime + opencv + numpy）。

## exe 打包

1. **入口必须是包外 `entry.py`**：`service/__main__.py` 内含包内相对导入（`from .app import ...`），
   直接作 PyInstaller 入口会报 `attempted relative import with no known parent package`。
   `entry.py`（项目根，已提交）以 `from service.__main__ import main` 方式加载，保留包上下文。
2. **资源收集**（`build_exe.py` 已配置，勿删）：
   - `--add-data templates;templates`、`--add-data libs/wgc_capture.dll;libs`
     （onefile 解压根 `_MEIPASS`，代码 `__file__` 上溯三级的相对路径恰好指向 `_MEIPASS/templates`、`_MEIPASS/libs`）；
   - `--collect-data rapidocr_onnxruntime`（OCR 模型在包内，缺失则 exe 运行时 OCR 找不到模型）。
3. **冒烟**：`--help` 必须含 `endpoint-file`/`state-dir`/`log-dir`；`--smoke-start` 时临时启动 exe 并请求 `/v1/health == READY`。
4. **已知陷阱**：
   - exe 常驻持有输出管道句柄 → 父进程等 EOF 永久卡住。脚本内所有 exe 子进程必须重定向 stdout/stderr。
   - PyInstaller onefile 是**父子进程模型**（bootloader 派生 app 子进程）→ 清理用 `taskkill /PID <bootloader> /T /F` 连树杀，单 terminate 会留子进程残留。
   - 构建期间 exe 临时实例勿手工残留（`Get-Process qq-desktop-service` 检查）。

## 参考资料

- [PACKAGING.md](references/PACKAGING.md)：exe 打包决策细节、资源路径表、conda 环境、运行命令、校验清单变更规则。
