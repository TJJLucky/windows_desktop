# qidian 唤醒与映射

调用方（客户端）不再自己打开 qidian 链接，也不自己维护「哪个商家对应哪个 QQ 接待人」。
它只把 **qidian 链接 + 该链接对应的公司信息** 交给本机服务，剩下的解析、唤醒、识别、
落映射全部由服务串行完成，避免多个 qidian 同时唤醒时接待人对应错乱。

## 为什么要放到服务端串行

qidian 链接被打开后，QQ 会把该商家的临时会话弹到会话列表顶部并激活它。识别
「当前接待人」只能读**当前激活行**，所以「打开链接」和「读激活行」必须是一段不可被打断的
临界区。服务端把整段唤醒作为一个 `qidian_wake` 任务投进 `Dispatcher` 单消费者队列，
与读名单、发送、读消息天然互斥（FIFO 排队），因此并发到达的多个 qidian 只会依次执行。

## 链路（`utils/qq/qidian.py`）

1. **链接校验** `normalize_link()`：只接受 `http(s)://qidianqq.hqew.com/GoClient`
   （精确主机 + 精确路径）；拒绝 `userinfo`、显式端口、其他路径、子域变体（如
   `qidianqq.hqew.com.evil.test`、`sub.qidianqq.hqew.com`）与其他协议（`tencent://`、
   `javascript:`）。规则与客户端 `isHqewContactUrl()` 对齐。违规 → `QQ_QIDIAN_LINK_INVALID`（HTTP 422）。
2. **解析目标 QQ 号** `resolve_target_qq()`：先取链接自身 query 参数 `QQ`；取不到时
   POST `https://qidianqq.hqew.com/QidianApi/User/GetQQLinkUrl`（把链接的全部 query 参数
   原样透传，`Content-Type: application/json`），从返回 `Data` 里的 `wpa.qq.com/msgrd?...&uin=`
   提取 uin。两者都拿不到 → `QQ_QIDIAN_QQ_UNRESOLVED`（HTTP 503），**不唤醒**。
   该接口视为尽力而为：只有在「链接没有 `QQ` 且接口也拿不到」时才硬失败。
3. **唤醒**：QQ 进程不存在时先走既有 `window_ops.start_qq()`；再 `os.startfile(链接)`
   交给默认浏览器打开（与客户端现网已跑通的路径一致，不反向构造 `tencent://` 链接）。
   打开失败 → `QQ_QIDIAN_WAKE_FAILED`。
4. **等会话稳定** `_wait_active_contact()`：轮询无障碍树会话名单，直到**连续两次读到同一份名单
   且只有唯一激活行**。名单已稳定却迟迟没有唯一激活行时，按具体原因失败（见下），不空等预算。
5. **识别接待人** `ContactNumberReader.identify_current()`：复制当前激活行的 QQ 号；群会话或
   不提供「复制 QQ 号」菜单项的会话（如官方号）返回 `null`，**不算失败**。
6. **落映射** `service/qidian_mapping.py`：按 `(siteId, supplierId)` 写入，旧值一并留在唤醒日志。

总等待上限 `WAKE_TIMEOUT_BUDGET_SECONDS`（约 25 秒，含窗口就绪等待），超时 → `QQ_QIDIAN_SESSION_NOT_STABLE`。

## 错误码

| 错误码 | 触发条件 |
| --- | --- |
| `QQ_QIDIAN_LINK_INVALID` | 链接不满足主机/路径/协议白名单（HTTP 422） |
| `QQ_QIDIAN_QQ_UNRESOLVED` | 链接没有 `QQ` 参数且 qidian 接口也解析不出目标 QQ 号 |
| `QQ_QIDIAN_WAKE_FAILED` | QQ 窗口就绪失败，或无法把链接交给默认浏览器 |
| `QQ_QIDIAN_SESSION_NOT_STABLE` | 总预算内没等到「稳定名单 + 唯一激活行」，或识别阶段失败 |
| `QQ_QIDIAN_ACTIVE_NOT_FOUND` | 名单稳定但读不到激活行 |
| `QQ_QIDIAN_ACTIVE_AMBIGUOUS` | 名单里出现多行激活（不做启发式猜测，直接失败） |
| `QQ_QIDIAN_QUEUE_TIMEOUT` | 排队等待超过放大后的上限（任务本身不会被取消） |

任何失败都**不写映射**，调用方不会拿到脏数据。

## 映射存储口径

`service/qidian_mapping.py`：标准库 `sqlite3` + 短连接 + 进程内 `threading.Lock`，与
`chat_history.py` / `command_ledger.py` 同构；默认落在账本同目录的 `qq-qidian-mappings.sqlite3`。

- `qidian_mapping`：主键 `(site_id, supplier_id)`，字段含 `link_hash / supplier_name /
  contact_name / qq_number / target_qq_number / source / updated_at / woke_epoch / wake_count`；
  另有 `link_hash` 索引便于反查。
- `qidian_wake_log`：每次唤醒一行（自增 id），同时保存本次与上一次的
  `contact_name / qq_number`，便于复盘「接待人换过没有」。
- **覆盖策略**：同一 `(siteId, supplierId)` 直接覆盖，不返回冲突提示；旧映射只进唤醒日志。
- **同链去重**：同一 `linkHash` 在 120 秒（`DEDUP_WINDOW_SECONDS`）窗口内重复请求直接复用
  上次结果（响应 `reused=true`），不重复唤醒；不同链接按 FIFO 排队。
  去重键以路由层 `normalize_link()` + `link_hash()` 算出的指纹为准（与日志查询键同一个值）。

## 接口

- `POST /v1/qidian:wake`：请求 `{link, siteId, supplierId, supplierName, targetId?, mpn?, batchId?}`
  （`targetId/mpn/batchId` 仅用于审计追溯，不参与映射）；响应 `linkHash / siteId / supplierId /
  supplierName / contactName / qqNumber? / targetQqNumber? / qqNumberVerified / activeStable /
  reused / wokeAt`。`qqNumberVerified=true` 表示识别到的接待人 QQ 号与链接解析出的目标 QQ 号一致。
- `POST /v1/qidian/mappings:query`：请求 `{siteId?, supplierId?, linkHash?}`（`linkHash` 与
  `(siteId, supplierId)` 二选一，缺两者 422）；响应 `found` + 映射字段（`source` 固定 `qidian_wake`）。
