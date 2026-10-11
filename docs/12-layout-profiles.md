# QQ 布局版本与模板

内置 `qq-9.9.35-52804-x64-white` 对应 **极简白 · 9.9.35-52804（64 位）**。默认资源只读，不能删除或覆盖。新版本从来源复制全部 12 张模板并替换指定 PNG；同一个 QQ 版本可以有多个主题/用户适配版本。已保存图片不可原地修改，名称和说明可修改。

## 持久化

服务 `state-dir/qq-layouts.sqlite3` 保存版本、PNG BLOB、当前启用 ID、来源 ID 和最近识别记录，SQLite `user_version=1`。新建副本在一个事务内提交；运行/更新 EXE 不覆盖该库。默认 PNG 来自 EXE 内置资源。

位置为物理像素，分别提供窗口内 `imageRect` 与虚拟桌面 `screenRect`；副屏可以是负坐标。`desktop` 返回整个虚拟桌面的边界和每个显示器的边界/DPI。前端用虚拟桌面原点和统一缩放绘图，不使用 CSS 屏幕尺寸推测桌面坐标。

重启后的历史识别记录标记 `service_restarted`，超过 30 秒标记 `expired`；窗口/显示器改变标记 `desktop_changed`。历史记录不会恢复成可以点击的运行缓存。正常运行的区域缓存按版本和窗口几何隔离，切换时清除区域、联系人及输入/消息对象缓存。

## HTTP

继续使用服务发布的本机动态端口，不新增任意路径、URL、脚本入口。

| 方法与路径 | 请求/行为 |
| --- | --- |
| GET `/v1/layout-profiles` | `{activeId, profiles}` |
| POST `/v1/layout-profiles` | `sourceId,name,qqVersion,theme,architecture,description,replacements`；创建后自动检测 |
| GET `/v1/layout-profiles/{id}` | 元数据和 `templates` 清单（key、label、width、height、sha256） |
| PATCH `/v1/layout-profiles/{id}` | 仅 `{name,description}` |
| DELETE `/v1/layout-profiles/{id}` | 删除自定义非启用版本；成功 204 |
| POST `/v1/layout-profiles/{id}:activate` | 启用并持久化；不触发识别 |
| GET `/v1/layout-profiles/{id}/templates/{key}/image` | `image/png` |
| GET `/v1/desktop-layout` | 虚拟桌面与显示器信息；不截图 |
| GET `/v1/layout-profiles/{id}/recognition` | 当前/历史位置缓存；不截图 |
| POST `/v1/layout-profiles/{id}:recognize` | 捕获当前 QQ 主窗口并匹配；不启用该版本 |

`replacements` 是固定模板 key 到 PNG Base64 的对象。不传替换项则完整复制来源图片；仅 PNG，单张最大 5 MiB，尺寸上限 4096×4096，不能全透明。`architecture` 为 `x64/x86/arm64`。

新建响应为 `{profile,recognition,recognitionError}`，HTTP 201 表示数据保存完成，识别失败不会回滚模板。客户端已占用 QQ 会话确认队列时传 `recognizeAfterSave:false`，提示 `LAYOUT_DESKTOP_BUSY`，稍后由用户重新识别。

识别响应包含 `profileId,snapshotId,capturedAt,valid,staleReason,window,desktop,templates,regions`。模板状态是 `matched/not_found/not_checked`；业务区域状态是 `matched/fallback/not_found`。非当前状态的复制按钮、发送按钮变体等不会被伪造为已命中；托盘旧模板仍保留但不参与当前 QQ 主窗口识别。

错误采用 `{"detail":"CODE"}`：默认保护、启用项保护、桌面占用/变化为 409；版本/模板不存在为 404；非法 PNG 或字段为 422；资源损坏为 503。

每次手动识别和保存后的自动识别，先调用与 `/v1/capture:check` 相同的 `ensure_qq_window_with_retry()`：恢复/唤起 QQ，按现有规则整理为当前显示器工作区左半屏，并确认 WGC 可用，然后截图匹配。就绪检查失败则终止，不使用之前的窗口坐标。准备、截图、匹配处于同一个桌面操作锁内；不切换联系人或发送消息。

## 页面

PriceAgent 设置 → **QQ 布局版本**。上传仅改变页面待保存内容；“另存为新版本”保存后自动识别，再把命中的模板绘制到真实桌面大小的空白画布。画布禁止拖动模板，不显示 QQ 聊天截图。只有用户明确点击“启用此版本”才改变正式操作使用的模板。

可读缓存每两秒刷新，不自动截图。QQ 任务占用时仍可查看、保存，不能执行检测/启用。适配范围是现有定位规则下的外观变化；结构变化需要另外调整定位规则。
