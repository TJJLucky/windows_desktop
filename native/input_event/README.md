# QQ native InputEvent module

这是当前 QQ 鼠标输入模块的 C++ 源码。Python 通过
`utils/core/mouse.py` 用 `ctypes.WinDLL(...)` 加载 x64 运行时文件
`libs/input_event.dll`；该 DLL 由本 Visual Studio 工程编译而来。

调用关系：

```text
QQ 业务代码
  -> utils/core/mouse.py（Python 适配与参数检查）
  -> libs/input_event.dll（C++ 导出函数）
  -> Windows SendInput（系统鼠标/键盘事件）
```

- Upstream: `https://github.com/QQPilotOrganization/QQPilot`
- Source directory: `VisionQQ_C/InputEvent`
- Snapshot commit: `d93f6694b4cdf90750b05052f76547621f29914f`
- Retrieved: 2026-09-29
- License: MIT; see [LICENSE](LICENSE).

该快照不包含 `InputEvent.vcxproj.user`，因为它是机器相关的 Visual Studio
个人配置。构建时在 `InputEvent.slnx` 或 `InputEvent.vcxproj` 中选择
`Release|x64`，将生成的 `InputEvent.dll` 复制为 `libs/input_event.dll`。
`.obj`、`.pdb` 和 `Release` 目录均为本机中间产物，已被 Git 忽略。
