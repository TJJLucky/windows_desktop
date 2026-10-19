# qidian 扫描后最终确认修复（2026-10-19）

分支 `codex/qq-reply-queue`，不改客户端网站采集逻辑，不增加桌面执行线程。

## 原因与修复

初次 qidian 唤醒核号成功后，`assert_unique_name` 为排除同名联系人可能滚动名单。此前随后直接检查激活行，目标可能已离开视口，使最终确认失败。窗口可截图不意味着目标激活行仍可从当前树中读取。

保留连续两次名称和网站目标 QQ 号一致的门槛。唯一性扫描后调用 `restore_verified_contact` 恢复目标，再读取新鲜激活行、再次复制号码，并检查最终激活名称未变化，全部成功后才返回持久化。定位与滚动扫掠接收原始 deadline，不重开等待预算；既有普通 activate 保留原有独立预算行为。无法保证单次底层 UIA/系统调用可被强制中断。

`resolve_verified` 位于现有 Dispatcher 串行操作中，覆盖唤醒、确认和映射写入。复用仍走无障碍树按唯一名称激活。失败保留 `restore_active`、`final_number`、`confirm_active` 等真实阶段，不推断鼠标干扰。日志从 metadata.audit 读取 batchId/targetId，与客户端请求关联。

## 真机证据

2026-10-19 12:37，通过真实 Electron 应用的原有 wake 接口打开历史真实链接：目标 `row-4:hqew:14`，网站 QQ `2881290148`，接待人为“陈伟洲 深圳市携诚达科技”。连续两轮核号与扫描后的最终核号一致，首次应用调用 9855 ms；第二次名称复用 2520 ms，`reused=true`。本轮未发送消息。

日志：`C:/Users/Administrator/AppData/Local/Temp/price-agent-qq-queue-20261019/expanded-final/service.log`。应用结果：同目录 `report.json`。关闭本次源码服务时退出码 0，记录 `automation.closed capture_released=true`。

Python 语法检查通过，无单元测试。不可滚动名单、同名歧义、重建、号码不符及目标离开视口尚未分别进行真机故障注入，不以本次正常路径成功代替这些验收。客户端真实批次因缺少模型 API Key 暂停，自动发送验收未执行。
