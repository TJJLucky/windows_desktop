# WGC 持续挂载采集与第一期联系人号码验收

本轮把 QQ 截图从"每次操作新建 WGC 会话"改为"QQ 窗口可用期间持续挂载、业务请求到来时按需读取"，
并用同一引擎上线的 `POST /v1/contacts/current:identify` 做真机验收：只识别当前激活行的 QQ 号，不发送消息。
改动范围是 `windows_desktop` 的原生采集、Python 封装、联系人号码模块与资源清理；不更新应用安装包。

## 构建

- Visual Studio 2026 Community（MSBuild 18）、v145 工具链、x64 Release、C++20、静态 CRT，NuGet `Microsoft.Windows.CppWinRT 3.0.260715.1` 已还原到 `native/wgc_capture/packages/`。
- `MSBuild.exe wgc_capture.vcxproj /t:Rebuild /p:Configuration=Release /p:Platform=x64` 干净重建通过，产物 `native/wgc_capture/x64/Release/wgc_capture.dll`（171008 字节），复制到 `libs/wgc_capture.dll`。
- 服务 EXE 由 `windows_desktop` 打包 Skill 重建：`python .skills/qq-desktop-package/scripts/build_exe.py --project-dir E:\Project\agent\windows_desktop --smoke-start`，111.6 MB，`--help`、`/v1/health` 与无参数启动冒烟全部通过。

## 原生契约

`WgcOpen / WgcRead / WgcClose / WgcClock100ns` 是新的内部接口，`WgcSnapshot` 保留旧签名并复用同一引擎，不存在两套截图实现。
`WgcFrameInfo` 固定 56 字节（`structSize` 由调用方先写 56），携带尺寸、会话代次 `generation`、代次内帧号 `frameNumber`、
WGC 帧时间与到达时间、错误码与 `hresult`。返回码 `1` 成功、`-2` 窗口不可用、`-3` 超时（缓冲里的旧数据不得使用）、
`-4` 缓冲不足、`-5` 会话已被替换、`-6` 设备丢失、`-7` 采集失败、`-8` 已关闭。

按钮点击、菜单点击等动作完成后记录本机 QPC 时间屏障，只接受屏障之后到达的帧（帧时间与到达时间都要晚于屏障）；
窗口尺寸变化重建帧池与纹理并递增 `generation`。像素相同的新帧算成功，但帧号或时间**不能**替代菜单、行身份与剪贴板校验。

## 验证结果

原生契约用例 `python tests/live_wgc_contract.py`：11/11 通过 —— 同窗口复用会话、像素相同的新帧、缓冲不足不写像素、
未来屏障拒绝旧帧直到超时、并发读取、尺寸变化重建代次、最小化恢复、隐藏显示重开、销毁替换与过期会话关闭、
`WgcSnapshot` 与持久会话共用引擎、重复关闭与关闭后读取。

采集耗时（20 次采样、间隔 2 秒，`debug/wgc-persistent-20261011/capture-engine-bench.json`）：

| 引擎 | 首次 | 热调用中位数 | P95 |
| --- | --- | --- | --- |
| 旧一次性 `WgcSnapshot` | 69.79 ms | 59.26 ms | 67.81 ms |
| 持续挂载按需读取 | 6.53 ms | 15.43 ms | 22.17 ms |

同一 `WgcSnapshot` 接口分别加载旧、新 DLL 各采样 20 次（2 秒间隔，`dll-snapshot-bench.json`）：旧 DLL 首次 66.00 ms、热调用中位数 61.97 ms、P95 66.76 ms；新 DLL 首次 6.92 ms、热调用中位数 9.33 ms、P95 11.71 ms。

静止窗口也能拿到新帧：`static-feasibility-final.json` 连续 12 次读取全部 `fresh=true`，像素摘要完全相同——
说明"像素相同"必须按新帧接受，不能靠像素变化判断画面更新。

真实 HTTP 端到端（每次间隔 2 秒，20 次，目标联系人 TJJ = `2183841407`，全部只复制号码、不发送）：

| 版本 | 首次 | 热调用中位数 | P95 | 结果 |
| --- | --- | --- | --- | --- |
| 旧 EXE（改动前构建） | 2915.86 ms | 1453.50 ms | 1798.08 ms | 20/20 返回 `2183841407` |
| 源码服务（本次改动） | 2121.94 ms | 781.39 ms | 855.85 ms | 20/20 返回 `2183841407` |
| 重建 EXE（本次改动） | 2148.48 ms | 769.49 ms | 817.69 ms | 20/20 返回 `2183841407` |

重建 EXE 相比旧 EXE：热调用中位数 −47.1%，P95 −54.5%，首次 −26.3%。EXE 已同步到
`price-agent-next/apps/desktop/resources/qq/qq-desktop-service.exe`（`npm run prepare:qq`）。

持续挂载 5 分钟（`observe/resources.json`，31 点采样）：CPU 平均 2.16% 单核（峰值 4.4%），
RSS 124.47 → 125.08 MiB，私有提交 609.53 → 610.16 MiB，句柄 1033 → 1024，线程 58 → 51，
GPU 3D 引擎平均 1.42%（峰值 1.53%）。最小化后恢复仍能识别 `2183841407`；正常退出码 0，端口文件已清理。

## 联系人号码链路

只保留一条链路：头像定位当前激活行 → 右键 → 模板定位"复制 QQ 号" → 左键 → 读剪贴板 → 精确核对号码。
菜单里没有"复制 QQ 号"时直接跳过，不回退 OCR、不猜坐标、不按昵称匹配。

真机发现并修正了两个具体问题：

- **群会话跳过是正确行为**：当激活行是群（实测"汉艺材质包售后群"）时，右键菜单是"复制群号"等条目，
  同一模板在该菜单上的最高匹配分只有 0.6221，低于 0.90 阈值，因此返回
  `{"skipped": true, "reason": "QQ_CONTACT_NUMBER_COPY_UNAVAILABLE", "qqNumber": null}`（HTTP 200），不点击任何条目。
  好友会话菜单上模板匹配分 0.9356，位置唯一。
- **鼠标悬停行造成的假歧义**：实测同一列表里未激活行 245 灰、激活行 226 灰、被鼠标悬停的未激活行 235 灰。
  右击复制号码后鼠标停在列表内，第二次调用就会同时出现 226 与 235 两个离群行，触发
  `QQ_CONTACT_NUMBER_ACTIVE_AMBIGUOUS`——这正是之前"第一次成功、之后必须重试"的原因。
  现在绝对色带是主判据，相对离群只做否决与兜底：色带命中行也在离群集合内就采纳色带结果，
  只有两路结论互相矛盾或色带内出现多行才判歧义。

回归：`python -m pytest -q` 205 项全部通过，含剪贴板旧值、菜单不存在、模板失败、窗口变化、采集超时、
代次变化、以及新增的悬停行与未知主题判定用例。

## 已知限制

- 本机 Windows 10 19045 实测 `Close + RoUninitialize` 后运行库异步回调可能进入已卸载的 `GraphicsCapture.dll`（0xc0000005），
  引擎因此把该运行库代码固定到进程退出；帧池、会话、图形设备、纹理、事件和线程仍正常释放，不累计每次采集的资源。
- 持续挂载不按空闲时间释放；窗口关闭、隐藏或最小化时停止采集，恢复后重新建立。
- 单次读取默认等待上限 300 ms，菜单整体等待 2 秒；超时、窗口变化与采集故障分别记录原因，不返回旧画面冒充新帧，不自动重放点击。
- `POST /v1/contacts/current:identify` 只识别当前激活行，不接受外部坐标、URL、脚本或文件路径；号码复制不可用时返回跳过原因。

## 证据

`E:\Project\agent\price-agent-next\debug\wgc-persistent-20261011\`：
`capture-engine-bench.json`、`static-feasibility-final.json`、`observe/`（资源与生命周期）、
`speed-before/`、`final-source/`、`final-after/`（HTTP 采样）、`group-session/`（群会话跳过）、
`hover-probe/hover.json`（行背景色）、`menu-live/`（菜单截图与模板匹配）。
