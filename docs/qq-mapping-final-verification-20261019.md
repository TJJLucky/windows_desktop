# qidian 扫描后最终确认修复（2026-10-19）

分支 `codex/qq-reply-queue`，不改客户端网站采集逻辑，不增加桌面执行线程。

## 原因与修复

初次 qidian 唤醒核号成功后，`assert_unique_name` 为排除同名联系人可能滚动名单。此前随后直接检查激活行，目标可能已离开视口，使最终确认失败。窗口可截图不意味着目标激活行仍可从当前树中读取。

保留连续两次名称和网站目标 QQ 号一致的门槛。唯一性扫描后调用 `restore_verified_contact` 恢复目标，再读取新鲜激活行、再次复制号码，并检查最终激活名称未变化，全部成功后才返回持久化。定位与滚动扫掠接收原始 deadline，不重开等待预算；既有普通 activate 保留原有独立预算行为。无法保证单次底层 UIA/系统调用可被强制中断。

`resolve_verified` 位于现有 Dispatcher 串行操作中，覆盖唤醒、确认和映射写入。复用仍走无障碍树按唯一名称激活。失败保留 `restore_active`、`final_number`、`confirm_active` 等真实阶段，不推断鼠标干扰。日志从 metadata.audit 读取 batchId/targetId，与客户端请求关联。

## 真机证据

2026-10-19 12:37，通过真实 Electron 应用的原有 wake 接口打开历史真实链接：目标 `row-4:hqew:14`，网站 QQ `2881290148`，接待人为“陈伟洲 深圳市携诚达科技”。连续两轮核号与扫描后的最终核号一致，首次应用调用 9855 ms；第二次名称复用 2520 ms，`reused=true`。本轮未发送消息。

日志：`C:/Users/Administrator/AppData/Local/Temp/price-agent-qq-queue-20261019/expanded-final/service.log`。应用结果：同目录 `report.json`。关闭本次源码服务时退出码 0，记录 `automation.closed capture_released=true`。

Python 语法检查通过，无单元测试。不可滚动名单、同名歧义、重建、号码不符及目标离开视口尚未分别进行真机故障注入，不以本次正常路径成功代替这些验收。该首轮客户端真实批次因缺少模型 API Key 暂停，自动发送验收未执行；配置密钥后的补验见下节。

## 配置密钥后补验与剪贴板误过滤修复（2026-10-19）

通过真实 Electron 应用恢复原授权批次，运行配置 `hasApiKey=true`。本轮实际发送 4 条，应用查询原命令均为 `SUCCEEDED`，再读取各自会话匹配 TJJ 正文与发送时间；提取保存 3 条真实历史报价。携诚达 QQ 2881290148 经应用重新核对身份后继续首询；同号多芯片会话在完成一个报价后推进到下一型号。完整客户端证据记录在客户端 `docs/project-map/qq-reply-queue-verification-20261019.md`。

### 实际错误和原因

12:58:44，服务日志：`contacts.refused.learned contact=鸿泰天成电子胡成/鸿泰天成电子 code=QQ_COPY_CLIPBOARD_UNCHANGED count=1`。此次消息复制失败约耗时 20.6 秒，整个历史读取约 23.3 秒。旧规则将“剪贴板未变化”一次失败当作永久不可复制证据，此后联系人被名单过滤。客户端历史读取投影为 `QQ_HISTORY_READ_FAILED`，刷新后提示“QQ 联系人已变化”。原始错误必须结合服务日志核对，不能据此断言联系人真的改变。

`utils/qq/contact_lookup.py` 从永久拒绝证据中移除 `QQ_COPY_CLIPBOARD_UNCHANGED`；`refusal_is_effective` 同时忽略不再属于有效拒绝码的旧学习记录，避免升级后仍受旧误判影响。该错误仍正常返回调用方，以便现有串行队列有限重试。未删除历史学习文件，也未改变系统官方号过滤或其他拒绝证据规则。`message.py` 注释同步调整。

### 修复后的真实结果

经真实应用 `qq.contacts(true)` 返回 `complete=true`、28 项，鸿泰天成恢复可见；随后 `qq.read` 成功返回商家“尊贵的VIP客户您好，很高兴为您服务！”及 TJJ 的 AT24C256C-SSHL-T 首询。无需手动删除本地学习记录。该会话旧人工阻塞状态保留，待用户重新核对继续，不自动放开其他身份阻塞。

首轮脚本在发送中关闭应用后，服务完成原命令，重启通过查询同一命令及消息回读恢复，无重复发送；这是明确的应用关闭场景，不代表所有网络超时已验收。其他旧映射失败未逐项重试，不能宣称全部商家成功。

证据根目录 `C:/Users/Administrator/AppData/Local/Temp/price-agent-qq-queue-20261019/`：原始误学在 `apikey-run/service.log`；修复后的完整联系人、成功读取、四条发送回读在 `apikey-evidence/report.json`，调用日志在同目录 `service.log`。最后批次 `interrupted`、未决发送为空；本次服务正常退出码 0，13:04:32 记录 `automation.closed capture_released=true`。本次 Electron/Next/服务均关闭，保留用户 QQ。

本轮两个 Python 修改文件语法检查和差异空白检查通过，无单元测试。UIA 曾出现瞬时 COM 订户错误并重试恢复，不把它隐去，也不将全部失败归因于鼠标干扰。
