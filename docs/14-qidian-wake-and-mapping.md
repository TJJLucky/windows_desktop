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
2. **解析目标 QQ 号** `resolve_target_qq()`：**优先取链接自身的 `QQ` 参数**，因为真机实测它就是
   QQ 客户端里显示的接待人 QQ 号。`QQ` 参数有三种真实形态（2026-10-15 从本机浏览器历史采样 30 条
   真实链接，全部命中 `source=link`）：
   - **明文号**（12/30）：`QQ=3004348344` → 直接就是接待人 QQ 号；
   - **msgrd 链接**（14/30）：`QQ=http%3A%2F%2Fwpa.qq.com%2Fmsgrd%3F...%26uin%3D2881381498...`
     → 取 `uin=`；
   - **wpa1 短链**（4/30）：`QQ=https%3A%2F%2Fwpa1.qq.com%2F<短码>...` → 跟随 302 落到企点落地页取
     `kfuin=`（⚠️ 企点客服/路由号，可能与客户端显示的接待人号不相等，见下）。

   `QQ` 参数拿不到号时才退回站点自己的 `POST https://qidianqq.hqew.com/QidianApi/User/GetQQLinkUrl`
   （GoClient 页面 JS 用的就是它：把链接全部 query 参数原样透传、`Content-Type: application/json`，
   缺的键补空串），从返回 `Data` 里再走同一套换算。两者都拿不到 → `QQ_QIDIAN_QQ_UNRESOLVED`（HTTP 503），
   **不唤醒**。接口只能**兜底、不能优先**：它返回的 `kfuin` 是企点路由号，实测与接待人号不等。
3. **唤醒**：QQ 进程不存在时先走既有 `window_ops.start_qq()`；再 `os.startfile(链接)`
   交给默认浏览器打开（与客户端现网已跑通的路径一致，不反向构造 `tencent://` 链接）。
   打开失败 → `QQ_QIDIAN_WAKE_FAILED`。
4. **等会话稳定** `_wait_active_contact()`：唤醒不是「打开浏览器就生效」——浏览器要起页、GoClient 再
   302/跳转、QQ 才把目标会话顶到最前（真机实测 3~5 秒）。所以**先等名单相对唤醒前发生变化**
   （预算 `WAKE_CHANGE_BUDGET_SECONDS` = 15s），再等名单在 `WAKE_SETTLE_SECONDS` = 1.5s 内不再变化
   且出现唯一激活行。名单已稳定却迟迟没有唯一激活行时按具体原因失败，不空等预算。
5. **识别接待人** `ContactNumberReader.identify_current()`：复制当前激活行的 QQ 号；群会话或
   不提供「复制 QQ 号」菜单项的会话（如官方号）返回 `null`，**不算失败**。会话刚被顶到最前时名单
   还在重绘、色带判定会瞬时读不到激活行，因此识别最多重试 `IDENTIFY_ATTEMPTS` = 3 次。
6. **生效判定（防脏映射）+ 落映射**：识别值 == 链接解析出的目标号 → `qqNumberVerified=true` 通过；
   若号对不上但**名单确实变了**（会话被顶到最前/新增一行）→ 也通过（`qqNumberVerified=false`，
   接待人可能换了号、或本身就是企点客服号）；只有「名单与唤醒前**一模一样**且号也对不上」才判定链接
   没生效，报 `QQ_QIDIAN_WAKE_FAILED` 并**拒绝写映射**，绝不让上一次的激活行冒充本次接待人。通过后由
   `service/qidian_mapping.py` 按 `(siteId, supplierId)` 覆盖写入。

总等待上限 `WAKE_TIMEOUT_BUDGET_SECONDS`（约 25 秒，含窗口就绪等待），超时 → `QQ_QIDIAN_SESSION_NOT_STABLE`。

> **企点客服号 vs 接待人号**：`wpa1.qq.com` 短链解析出的是企点路由号 `kfuin`，真机实测与客户端里
> 显示的接待人号**不相等**（例：链接目标 `3009004623`，识别到 `800885759`）。此时
> `qqNumberVerified=false`，映射仍以 `contactName`（真机上就是接待窗口名）落库；要求号完全一致的
> 调用方需自行按 `qqNumberVerified` 裁决。

## 错误码

| 错误码 | 触发条件 |
| --- | --- |
| `QQ_QIDIAN_LINK_INVALID` | 链接不满足主机/路径/协议白名单（HTTP 422） |
| `QQ_QIDIAN_QQ_UNRESOLVED` | 链接没有 `QQ` 参数且 qidian 接口也解析不出目标 QQ 号 |
| `QQ_QIDIAN_WAKE_FAILED` | QQ 窗口就绪失败、无法把链接交给默认浏览器，或链接未生效（名单与唤醒前完全一致且 QQ 号对不上，拒绝写映射） |
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

## 真机验证（2026-10-15）

样本：从本机 Edge 历史（`urls` 表，`url like '%qidianqq.hqew.com%'`）采出 **30 条真实链接**
（明文 12 / msgrd 14 / wpa1 4）。离线解析：**30/30 全部命中 `source=link`**。

在线唤醒（真实链接、服务端 `POST /v1/qidian:wake`，与 `/v1/contacts/current:identify` 交叉核对）：

| 形态 | supplierId | HTTP | 耗时 | contactName | qqNumber | targetQqNumber | verified |
| --- | --- | --- | --- | --- | --- | --- | --- |
| msgrd | `live-real-msgrd-a` | 200 | 6.03s | 陈伟洲 深圳市携诚达科技 | 2881290148 | 2881290148 | ✅ true |
| msgrd | `live-real-msgrd-c` | 200 | 21.8s* | 芯世交科技 | 2885099490 | 2885099490 | ✅ true |
| plain | `live-real-plain-b` | 200 | 27.4s* | 杰兴电子科技-单片机销售 | 2885512390 | 2885512390 | ✅ true |
| wpa1 | `live-wpa1-001` | 200 | 5.07s | 森蓝微电子 | 800885759 | 3009004623 | ⚠️ false |

\* 并发场景：3 条不同链接同时提交，服务端单消费者队列串行执行，`wall=27.4s`，每条的 `qidian.wake.target`
日志都出现在上一条 `qidian.wake.done` 之后（无交叉），3 条映射各归各自 uin。

- **同链去重**：同一链接 120s 内再提交 → HTTP 200、`reused=true`、耗时 0.02s，未二次唤醒。
- **参数违规**：非白名单主机 / 协议 / 路径 / 带端口 → 422 `QQ_QIDIAN_LINK_INVALID`。
- **解析失败**：白名单链接但无 `QQ` 参数且接口无结果 → 503 `QQ_QIDIAN_QQ_UNRESOLVED`，不唤醒。
- **早期坑（已修）**：曾用伪造链接 `?QQ=110001335`（缺 `BHqewUserID`）测试——能通过校验但**不会真弹会话**，
  识别到的是上一次的激活行，写出脏映射。故新增「生效判定」：名单未变且号对不上就拒绝写映射。
