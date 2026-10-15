# qidian 唤醒与映射

调用方（客户端）不再自己打开 qidian 链接，也不自己维护「哪个商家对应哪个 QQ 接待人」。
它只把 **qidian 链接 + 目标 QQ 号 + 该链接对应的公司信息** 交给本机服务，剩下的唤醒、识别、
落映射全部由服务串行完成，避免多个 qidian 同时唤醒时接待人对应错乱。

其中 **目标 QQ 号由调用方显式传入，且是必填**：客户端在打开链接之前，已经从华强搜索结果页行内的
`a.customerqq[qq]` 读到商家 QQ 号，它才是权威来源。服务端**完全不解析链接里的号**（原因见
「为什么服务端不解析链接里的号」）。

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
2. **取目标 QQ 号** `require_target_qq(provided)`：**只认调用方传入的 `targetQqNumber`**。
   它就是客户端从华强搜索结果页行内 `a.customerqq[qq]` 读到的商家 QQ 号，与接待人是同一个号。
   服务端**不取链接里的号、也不调站点接口**：缺失或格式非法 → `QQ_QIDIAN_QQ_UNRESOLVED`
   （HTTP 契约层对必填与 `^[1-9][0-9]{4,19}$` 直接 422；服务内另留一条 503 防御分支，用于绕过
   契约的内部调用），**不唤醒**。
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
6. **生效判定（防脏映射）+ 落映射**：识别值 == 本次的目标号 → `qqNumberVerified=true` 通过；
   若号对不上但**名单确实变了**（会话被顶到最前/新增一行）→ 也通过（`qqNumberVerified=false`，
   接待人可能换了号、或本身就是企点客服号）；只有「名单与唤醒前**一模一样**且号也对不上」才判定链接
   没生效，报 `QQ_QIDIAN_WAKE_FAILED` 并**拒绝写映射**，绝不让上一次的激活行冒充本次接待人。通过后由
   `service/qidian_mapping.py` 按 `(siteId, supplierId)` 覆盖写入。

总等待上限 `WAKE_TIMEOUT_BUDGET_SECONDS`（约 25 秒，含窗口就绪等待），超时 → `QQ_QIDIAN_SESSION_NOT_STABLE`。

> **企点客服号 vs 接待人号**：链接里带的也可能是企点路由号 `kfuin`，真机实测与 QQ 客户端里
> 显示的接待人号**不相等**（例：`wpa1.qq.com` 短链的 `3009004623`，`identify` 读到 `800885759`）。
> 这正是服务端不解析链接的原因：判定接待人一律用调用方从网站读到的商家 QQ 号。
> 号对不上时 `qqNumberVerified=false`，映射仍以 `contactName`（真机上就是接待窗口名）落库；
> 要求号完全一致的调用方需自行按 `qqNumberVerified` 裁决。

## 为什么服务端不解析链接里的号

2026-10-15 从本机 Edge 历史采样 **30 条真实链接**（明文 12 / msgrd 14 / wpa1 4），逐条核对
「链接解析出的号」与「QQ 客户端里 `identify` 读到的接待人号」：

| 链接形态 | 链接/接口能拿到的号 | 是不是接待人 |
| --- | --- | --- |
| 明文号 `QQ=3004348344` | 3004348344 | 是（实测与 `identify` 一致） |
| `wpa.qq.com/msgrd?...&uin=` | uin | 是（实测一致） |
| `wpa1.qq.com/<短码>`（302 后 `kfuin`） | 企点**路由/客服号** | 否（真机：目标 `3009004623`，`identify` 读到 `800885759`） |
| 站点 `GetQQLinkUrl` 返回的 `kfuin` | 企点**路由/客服号** | 否（同上） |

也就是说「链接/接口拿到的号」里存在**不等于接待人**的企点路由号：用它做目标号会让
`qqNumberVerified` 永远为 false，映射也失去「号 == 接待人」这层保证。而华强搜索结果页行内
`a.customerqq[qq]` 本来就是商家对外公布的接待 QQ 号，客户端在准备询价时已经拿到它，
因此**由调用方传号**才是权威来源。

服务端因此**彻底移除了链接解析**（不再有 `QQ` 参数兜底、不再调 `GetQQLinkUrl`、不再跟随
`wpa1` 短链 302）：少一条 URL 抓取路径，就少一处失败面与一处「号可能不对」的歧义。
`targetQqNumber` 是必填字段，缺号直接 422，服务端不会退化成「猜一个号去唤醒」。

## 错误码

| 错误码 | 触发条件 |
| --- | --- |
| `QQ_QIDIAN_LINK_INVALID` | 链接不满足主机/路径/协议白名单（HTTP 422） |
| `QQ_QIDIAN_QQ_UNRESOLVED` | `targetQqNumber` 缺失或格式非法（HTTP 契约层 422；绕过契约的内部调用由服务内防御分支映射成 503） |
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
  上次结果（响应 `reused=true`），不重复唤醒、不写唤醒日志、不加 `wakeCount`；但会用本次请求的
  `targetQqNumber` 调 `refresh_target()` 刷新映射行，保证响应与落库同号；不同链接按 FIFO 排队。
  去重键以路由层 `normalize_link()` + `link_hash()` 算出的指纹为准（与日志查询键同一个值）。

## 接口

- `POST /v1/qidian:wake`：请求 `{link, siteId, supplierId, supplierName, targetQqNumber, targetId?,
  mpn?, batchId?}`（`targetId/mpn/batchId` 仅用于审计追溯，不参与映射）。
  **`targetQqNumber` 是必填字段**：调用方从华强搜索结果页读到的商家 QQ 号，必须满足
  `^[1-9][0-9]{4,19}$`，缺失或格式非法一律 422；服务端只用它判定接待人，不解析链接里的号。
  响应 `linkHash / siteId / supplierId / supplierName / contactName / qqNumber? / targetQqNumber /
  qqNumberVerified / activeStable / reused / wokeAt`。
  `qqNumberVerified=true` 表示识别到的接待人 QQ 号与**本次请求传入的目标号**一致。
- `POST /v1/qidian/mappings:query`：请求 `{siteId?, supplierId?, linkHash?}`（`linkHash` 与
  `(siteId, supplierId)` 二选一，缺两者 422）；响应 `found` + 映射字段（映射行的 `source` 固定
  `qidian_wake`，`targetQqNumber` 是最近一次调用方传入的商家 QQ 号——真唤醒与复用命中都会刷新它，
  `wakeCount` 只统计真唤醒）。

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
| plain | `live-caller-plain` | 200 | 7.77s | 亚洲星电子 | 3004348344 | 3004348344（caller） | ✅ true |
| wpa1 | `live-caller-wpa1` | 200 | 16.45s | 森蓝微电子 | 800885759 | 800885759（caller） | ✅ true |

\* 并发场景：3 条不同链接同时提交，服务端单消费者队列串行执行，`wall=27.4s`，每条的 `qidian.wake.target`
日志都出现在上一条 `qidian.wake.done` 之后（无交叉），3 条映射各归各自 uin。

- **同链去重**：同一链接 120s 内再提交 → HTTP 200、`reused=true`、耗时 0.02s，未二次唤醒。
- **参数违规**：非白名单主机 / 协议 / 路径 / 带端口 → 422 `QQ_QIDIAN_LINK_INVALID`。
- **缺号**：不传 `targetQqNumber` → HTTP **422**（`missing`）；`targetQqNumber="abc"` → HTTP **422**
  （`string_pattern_mismatch`）；两种情况都不唤醒、不写映射。
- **调用方传号（2026-10-15 新增，真机跑通）**：
  - `targetQqNumber` 生效：日志 `qidian.target qq=...`、响应 `targetQqNumber` 回显、`qqNumberVerified=true`；
  - **决定性证据**：用 `wpa1` 短链（链接里的号是企点路由号 `3009004623`）+ 调用方传 `800885759` →
    响应 `qqNumberVerified=true`、`contactName=森蓝微电子`，说明判定只用调用方传的号；
  - 复用窗口内重提：`reused=true`，`targetQqNumber`/`qqNumberVerified` 取**本次请求**的值，
    并把该目标号刷进映射行的 `target_qq_number`（响应与落库必须同号），`wake_count` 不增加
    （复用等于「这次没有真的唤醒」）；
  - 移除链接解析后的回归（2026-10-15 真机）：不带 `targetQqNumber` → 422 `missing`；`"abc"`/`"1234"`/
    `"0123456"` → 422 `string_too_short` / `string_pattern_mismatch`，都不唤醒、不写映射；
  - **真机端到端（2026-10-15 20:55，QQ 在线）**：明文链接 + 调用方传 `3003790385` → HTTP 200、
    `reused=false`、`contactName=粤能半导体（深圳）`、`qqNumber=3003790385`、`qqNumberVerified=true`、
    耗时 9.09s；服务端日志 `qidian.target qq=3003790385` → `qidian.wake.done ... verified=True`，
    全程没有任何「解析链接」的分支被走到。
- **观察到的抖动（与本次改动无关，客户端应容错）**：同一条会话在很短时间内被第二次唤醒时，可能出现
  名单已稳定但**读不到激活行** → 503 `QQ_QIDIAN_ACTIVE_NOT_FOUND`（真机复现 2 次，均发生在同一
  `supplierId` 连续唤醒时）。调用方按 503 重试或回退人工确认即可，服务端不写脏映射。
- **早期坑（已修）**：曾用伪造链接 `?QQ=110001335`（缺 `BHqewUserID`）测试——能通过校验但**不会真弹会话**，
  识别到的是上一次的激活行，写出脏映射。故新增「生效判定」：名单未变且号对不上就拒绝写映射。
