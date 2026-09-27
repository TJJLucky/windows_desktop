# WGC 截图与 DPI 约定

## 截图来源

窗口像素只来自 `WGCCapture` 对明确 HWND 的 WGC 后台截获，不使用桌面或前台截屏。当前实现每次操作创建一次 WGC 快照，结束后释放 D3D/WGC 资源。

## DPI 初始化

`utils/__init__.py` 在包加载时调用：

```python
screenshot.ensure_dpi_aware()
```

优先使用 per-monitor V2，失败后依次降级到 per-monitor 和系统 DPI aware。

## 坐标约定

进程设置为 DPI-aware 后：

- `GetWindowRect` 返回物理像素。
- WGC 返回物理像素。
- `SetCursorPos` 和鼠标事件使用物理像素。
- 截图内区域坐标不能除以缩放系数。

## 当前已知限制

`getDPI()` 使用桌面 DC 读取 DPI，多显示器混合缩放场景不能保证等于目标窗口所在显示器 DPI。相关路径在副显示器或不同缩放比例下需要专门验证，必要时改为 `GetDpiForWindow` 或目标显示器 DPI。

## 缓冲区

`calc_buf_size()` 根据窗口矩形和 DPI 估算 WGC 输出缓冲区。任何 DPI 计算变化都必须同步验证缓冲区足够大，避免 WGC 返回失败或异常。