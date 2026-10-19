# 2026-10-18 联系人查询真机复盘

## 本次改动

- 普通按名查询先读稳定无障碍树，命中完整可见且当前树中唯一的行时直接返回，不执行滚轮操作，不以全表唯一性检查阻塞只读查询。
- 查询复用这次稳定快照，避免命中后再次枚举。目标不可见才进入原有定位流程。
- `activate` 仍在点击前执行完整同名核验；首次企点号码验证后的核验也保留。本次没有将可见唯一视为完整身份凭证。
- COM 对象改为指针有效性检查；名单读取与滚动查询共用容器获取。未找到时记录 hwnd、窗口名、窗口类，不再解引用空元素。
- ScrollPattern 获取前检查能力属性。未发布接口不等于不可滚动，也不等于树不可读取。
- 普通查询只有容器明确报告不可滚动才声明 complete=true；未提供滚动接口时保守返回 false。显式 all=true 仍使用既有全量扫描路径。

## 真机证据（北京时间）

使用 Conda qq-desktop-service 环境，通过真实 HTTP /v1/contacts:query 调用；没有模拟 UIA、没有单元测试、没有网站请求、没有发送消息。

11:16 修改前 D_Isaac 查询服务耗时 2104.88 ms，重复读取当前名单并尝试滚轮核验。
15:03 首次查询失败：窗口可截图，但会话列表未读到；错误明确为 pane '会话列表' not found。当时尚未补上列表缺失的有界等待。后续补查控制台与 Windows 进程创建时间，确认 QQ 在 15:03:56 刚启动；失败使用 HWND 327994，稍后成功使用 HWND 132152。此前仅凭应用日志无法确认冷启动的判断已由新增证据修正，详细证据见末节。
15:05 最终补丁实测（客户端计时，含本机 HTTP）：

| 请求 | 耗时 ms | HTTP | 条数 | complete |
| --- | ---: | ---: | ---: | --- |
| 普通名单，服务首次调用 | 1329 | 200 | 11 | false |
| D_Isaac | 243 | 200 | 11 | false |
| D_Isaac 再次查询 | 238 | 200 | 11 | false |
| TJJ | 237 | 200 | 11 | false |
| all=true | 1363 | 200 | 11 | true |

树含14行，过滤3个不可复制会话。可见查询日志 contacts.query mode=visible。全量结果沿用既有滚轮边界判断，本次短名单通过，不代表长列表、虚拟化或滚轮被遮挡时已验证。

## 验证边界

Python AST、service.app 导入、git diff --check 通过。本次仅改服务 Python 与文档，无客户端改动，不重复客户端构建。
首次真实企点唤醒、号码匹配落库、缓存映射复用、长列表同名冲突仍未完成本轮验收。不得将名单查询提速当作映射链路已通过。
日志：C:/Users/Administrator/AppData/Local/Temp/qq-chain-20261018/logs/qq-desktop-service.log
最终测量：同目录上一级 verification-final.json。前一轮失败结果保留在 verification-fixed.json。

## 22:09 冷就绪等待修复后的真机回归

修复 `utils/qq/contact_lookup.py::read_pane()`：截图窗口已经可用时，如果 UIA 暂时还没有发布“会话列表”节点，服务在原有 1.5 秒稳定预算内持续重新读取；每次缺失都会清空上一份名单指纹，避免把旧名单与新树拼成“连续稳定”。预算耗尽仍抛出原始 `QQ_ACCESSIBILITY_UNAVAILABLE`，不会把缺失列表当成空联系人列表。

使用真实 QQ 窗口调用 `POST /v1/contacts:query`，未模拟 UIA、未使用单元测试、未访问网站、未发送消息：

- 首次查询返回 24 个联系人，耗时 1661.08 ms；
- 最小化后请求自动恢复 QQ 窗口并返回 24 个联系人，耗时 652.45 ms；
- 后续稳态查询返回 24 个联系人，耗时 254.91 ms；
- 未读字段正常返回：`！` 为 2 条，`优鑫胜（实单低价支持）` 为 1 条；
- 服务退出日志确认 `automation.closed capture_released=true`。

本次 QQ 已经运行且列表可读，未再次重启 QQ，因此没有把这组结果描述为完整冷启动验收。此前 15:03 的失败证据仍说明：WGC 截图成功只代表窗口可捕获，不能代表 UIA 会话列表已准备好。

证据：`%TEMP%\qq-pane-readiness-live-20261018\evidence.json`、`%TEMP%\qq-pane-readiness-live-20261018-console.log`。

## 22:16 列表真实缺失与恢复验收

通过实际点击 QQ 左侧联系人页制造“主窗口可见、会话列表节点不存在”的界面状态，没有替换 UIA 函数或模拟接口响应。接口与全部 UIA 读取仍走原 Dispatcher；临时验收脚本的点击模拟用户在等待过程中切回消息页。

- 持续停留联系人页：8 轮读取失败，`contacts.pane.timeout ... elapsed_ms=1531.0`，HTTP 503，总耗时 1586.78 ms；错误保留 `QQ_ACCESSIBILITY_UNAVAILABLE` 和 `pane '会话列表' not found (null UIA element)`，没有成功返回空名单。
- 在下一请求日志出现 `contacts.pane.wait` 后点击消息页：同一请求内自动恢复，`contacts.pane.recovered ... failures=1 rows=26 elapsed_ms=532.0`，HTTP 200，总耗时 577.32 ms；过滤后返回 24 个联系人。
- 恢复后的普通查询：HTTP 200，275.71 ms。
- 最后恢复消息页，22:16:27 正常退出服务，日志 `automation.closed capture_released=true`。

这是实际节点缺失路径的验收，不是 QQ 进程冷启动验收。切页能够产生相同报错，进一步说明历史报错本身不足以定位 QQ 内部的具体成因。持续停在非消息页不会由本补丁自动切页，仍按时限明确报错。1.5 秒是轮询预算，正在执行的同步 COM 读取无法由此计时器强制中断，实测略超预算。

证据：`%TEMP%/qq-pane-missing-live-20261018/evidence.json`、`%TEMP%/qq-pane-missing-live-20261018-console.log`。


## 启动阶段窗口误判与遮挡对照（补充调查）

本轮仅补充诊断和记录，没有修改运行代码。控制台日志与 Windows 进程创建时间确认：历史失败发生在 QQ 实际启动之后；服务把启动阶段可截图的 QQ 窗口过早视为可读取会话名单的窗口。

| 北京时间（2026-10-18） | 证据 |
| --- | --- |
| 15:03:56.311 | 控制台：未检测到 QQ 进程，唤起登录（需用户手动登录/扫码） |
| 15:03:56.739343 | Windows CreationDate：QQ.exe PID 36924 创建 |
| 15:03:57.529 | QQ 可见窗口共 0 个 |
| 15:03:58.349 | PID 36924 出现一个标题为 QQ 的可见窗口 |
| 15:03:58.825 | WGC capture.image 成功 |
| 15:03:58.998 | accessibility.connect hwnd=327994 成功 |
| 15:03:59.027–15:03:59.149 | 三次读取均报 pane '会话列表' not found |
| 15:03:59.177–15:03:59.803 | 四次请求返回 HTTP 503，QQ_ACCESSIBILITY_UNAVAILABLE |
| 15:04:45.645 | accessibility.connect hwnd=132152，窗口句柄已不同 |
| 15:04:45.710 | accessibility.enumerate rows=14，elapsed_ms=64.30 |
| 15:04:45.855 | 联系人查询返回 HTTP 200 |

精确失败位置是 `utils/qq/accessibility.py::_pane_element()`：根节点连接成功，但按名称查找不到“会话列表”后代节点。不能将其描述为整棵 UIA 树无法访问，也不是滚动接口缺失。

就绪条件缺口在 `utils/qq/window_ops.py`：`_main_window_candidates()` 按窗口标题 QQ 选取候选，`_snapshot_and_verify()` 以截图非空确认窗口可用，没有确认消息页面或会话列表容器已存在。启动阶段窗口因此可以提前通过。未留存失败瞬间的截图，无法确定其具体显示登录页、加载页还是其他启动界面；两次采样之间存在空档，不能据此声称启动耗时 46 秒。

使用真实 QQ 进行遮挡对照，所有 UIA/QQ 读取都在原 Dispatcher 单消费者执行。临时原生置顶窗口覆盖 QQ 矩形，WindowFromPoint 在中心返回覆盖窗口 HWND 4328050，确认遮挡实际生效。四轮结果如下：

| 状态 | UIA 后代节点数 | 会话列表 | WGC 截图 | 过滤后联系人数 | query 耗时 ms |
| --- | ---: | --- | --- | ---: | ---: |
| 基线 | 332 | 存在 | 成功 | 24 | 342.55 |
| 完整覆盖 QQ | 332 | 存在 | 成功 | 24 | 334.54 |
| 继续覆盖 1 秒 | 332 | 存在 | 成功 | 24 | 344.79 |
| 移除覆盖窗口 | 332 | 存在 | 成功 | 24 | 341.63 |

当前真实 UIA 根节点为 HWND 132152、name=QQ、class=Chrome_WidgetWin_1、PID=36924；会话列表节点 class 含 recent-contact-list。四轮窗口均可见且未最小化，QQ 无须成为前台窗口即可读取。本机短时遮挡没有复现树读取失败；不外推为所有机器、所有遮挡时长均可成功。

既有 1.5 秒等待只缓解同一窗口内的列表短暂缺失：`read_pane(hwnd)` 持续使用传入句柄，不会在等待期间重新寻找替换后的主窗口，因此不能声称已经完整修复本次启动场景。后续最直接的处理是：在同一个串行任务内等待真正的会话列表容器，并在候选窗口更换时重新绑定句柄；到期保留真实失败，不返回伪空列表。登录等待与名单稳定等待需要区分。

诊断证据：

- `%TEMP%/qq-chain-20261018/logs/qq-desktop-service.console.log`
- `%TEMP%/qq-chain-20261018/logs/qq-desktop-service.log`
- `%TEMP%/qq-uia-cause-20261018/evidence.json`
- `%TEMP%/qq-uia-cause-20261018/process-times.json`
- `%TEMP%/qq-uia-cause-20261018-console.log`

临时遮挡窗口已销毁，诊断 worker 已结束，日志确认 `DIAGNOSTIC_CLOSED capture_released=true`。没有重启 QQ、发送消息、访问网站或新增单元测试。本轮未进行新的冷启动复现实验。
