# WGC Capture DLL

Windows Graphics Capture 的单窗口持续采集底座。首次打开建立独立 MTA 线程；没有空闲释放。帧到达事件唤醒线程，两个 WGC 缓冲及时关闭，只保留最新 GPU 纹理；业务读取时才复制到 CPU。

## 构建与部署

- Visual Studio **2026 Community / v145**，x64 Release、C++20、静态 CRT。
- Windows 10 SDK；NuGet `Microsoft.Windows.CppWinRT 3.0.260715.1`。
- 在 Visual Studio 还原 NuGet 包到本目录 `packages/`。包缺失时构建会明确失败。

```powershell
& 'C:/Program Files/Microsoft Visual Studio/18/Community/MSBuild/Current/Bin/MSBuild.exe' wgc_capture.vcxproj /p:Configuration=Release /p:Platform=x64
```

产物 `x64/Release/wgc_capture.dll`。退出正在使用 DLL 的服务后，复制到项目 `libs/wgc_capture.dll`；真实接口验收后再重建 EXE。不要向安装包直接塞入独立 DLL。

## C ABI（cdecl，x64）

| 导出 | 参数与行为 |
|---|---|
| `int WgcOpen(HWND, uint64_t* session)` | 同一可用窗口复用会话；恢复后创建新会话。一次只持有一个窗口。 |
| `int WgcRead(uint64_t session, uint8_t* buf, uint32_t capacity, int64_t barrier100ns, uint32_t timeoutMs, WgcFrameInfo* info)` | 读取屏障之后的新帧，像素为连续 BGRA，无行填充。默认由 Python 限定 300ms。 |
| `int WgcClose(uint64_t session)` | 等待线程清理并退出。重复关闭安全，过期会话不能关闭新会话；0 关闭当前会话。 |
| `int64_t WgcClock100ns()` | 本机 QPC 单调时钟，100ns 单位，用于动作完成后的屏障。 |
| `int WgcSnapshot(HWND, uint8_t*, int capacity, int* width, int* height)` | 保留旧签名和 1/0 返回值，复用同一引擎；空缓冲查询尺寸也等待有效新帧。 |

`WgcFrameInfo` 默认对齐，大小 **56** 字节；调用方先设置 `structSize=56`：

```cpp
struct WgcFrameInfo {
    uint32_t structSize, width, height, requiredBytes;
    uint64_t generation, frameNumber;
    int64_t frameTime100ns, arrivalTime100ns;
    int32_t error, hresult;
};
```

`generation` 在真实尺寸变化或会话重建后改变；`frameNumber` 仅在该代次内递增。第一帧若与 `GraphicsCaptureItem.Size()` 不一致，会在本代次内静默采用实际像素尺寸，不产生代次跳变；之后的尺寸变化重建帧池和纹理并递增 `generation`，随后仍返回可用新帧（不报 -5）。`frameTime100ns` 来自 WGC `SystemRelativeTime`，`arrivalTime100ns` 来自帧事件的 QPC。两者均须大于屏障；这可防止本机 WGC 时间戳略超前时误收缓存画面。像素相同的新帧可以成功；帧号或时间不能代替菜单、行身份和剪贴板校验。

| 返回值 | 含义 |
|---|---|
| 1 | 成功 |
| -1 | 参数/结构大小不正确 |
| -2 | 窗口关闭、隐藏、最小化或 PID 改变 |
| -3 | 等待超时；不得使用缓冲中的旧数据 |
| -4 | 缓冲不足，`requiredBytes` 给出所需容量；不写入像素 |
| -5 | 会话标识不匹配（已被新会话替换）；当前业务必须停止并重新核对 |
| -6 | 图形设备丢失 |
| -7 | 采集失败，详见 `hresult` |
| -8 | 已关闭 |

## 生命周期与 Python

`utils/core/screenshot.py` 的 `WGCCapture` 统一持有会话、锁和缓冲。`capture(hwnd)` 保留图片/None 契约；`capture_frame` 返回独立拥有 RGB 数据的图片和元数据，失败抛明确异常。业务截图不共享可变像素。

窗口事件停止不可用窗口的采集；尺寸变化重建帧池和纹理。设备/采集故障直接返回，不在当前读取中隐式重试。HTTP 服务退出先停止 Dispatcher，再调用 `WGCCapture.release()`。`DllMain` 不 join、不关闭采集。

本机 Windows 10 19045 实测 `Close + RoUninitialize` 后运行库异步回调可能进入已卸载的 `GraphicsCapture.dll`（0xc0000005）。引擎将该运行库代码固定到进程退出；帧池、会话、图形设备、纹理、事件与线程仍正常释放。该映射只影响代码模块寿命，不累计每次采集的资源。

## 验证

本项目不做单元测试。原生采集改动必须在真实窗口做真机验收：确认持续挂载复用会话、窗口恢复后重新建立、动作屏障拒绝旧帧、尺寸变化重建与失败码符合表内契约。QQ 的真实 HTTP 复制、2 秒间隔采样及资源观察结果记录在 `docs/wgc-persistent-validation.md`。
