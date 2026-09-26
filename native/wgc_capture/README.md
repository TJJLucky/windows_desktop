# WGC Capture DLL

基于 Windows Graphics Capture API (WinRT) 的后台窗口截图 DLL，适配 Python 无消息循环场景。

## 构建

- **IDE**: Visual Studio 2022 (x64)
- **依赖**: C++/WinRT 3.0.260715.1 (NuGet 自动还原)
- **目标**: x64 Release

```powershell
msbuild wgc_capture.vcxproj /p:Configuration=Release /p:Platform=x64
```

## DLL 导出接口

| 函数 | 说明 |
|------|------|
| `InitCapture(HWND, cropX, cropY, cropW, cropH)` | 初始化 D3D + WGC 捕获，可选 ROI 裁剪 |
| `GetLatestFrame(buf, size)` | 拉取最新一帧 BGRA 数据到缓存区 |
| `CleanupCapture()` | 释放所有 D3D 和 WGC 资源 |
| `CaptureWindow(HWND, buf, bufSize, outW, outH)` | **一键截图**：初始化 → 轮询等待帧 → 读取 → 释放 |

## 技术特性

- 回调 + 主动 `TryGetNextFrame` 轮询双通路，无需 Windows 消息循环
- 硬件渲染失败自动降级 WARP 软渲染
- ROI 边界钳位，防越界黑屏
- 窗口缩放自适应重建帧池与暂存纹理
- 窗口最小化尺寸归零安全防御
- 复用 D3D 延迟上下文，减少 GPU 开销

## 部署

编译产物 `x64/Release/wgc_capture.dll` 拷贝至:

```
client-runtime/python-runtime/src/agent_runtime/integrations/windows_desktop/libs/
```
