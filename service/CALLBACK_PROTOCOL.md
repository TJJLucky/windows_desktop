# 统一 Agent 的 QQ 回调协议

本扩展在原同步接口之外增加持久订阅、异步发送和回调投递。旧调用不传身份扩展字段时，响应 JSON 和同步发送入口保持原样。QQ 服务只执行规则与界面操作，不调用 LLM，不保存询价业务进度。

## 接口

| 接口 | 作用 |
| --- | --- |
| `GET /v1/capabilities` | 声明 `callbacks.v1`、`identity.v1`、`deferred-commands.v1` |
| `POST /v1/contacts:query?identity=true` | 返回稳定服务身份、原始证据和顶层 `identityWarnings` |
| `POST /v1/commands/read` | 传 `sessionId`、`conversationId` 后返回完整标题、持久消息和基线 |
| `PUT /v1/subscriptions/{subscriptionId}` | 持久登记消息或命令完成订阅，允许重启后更新回调端口 |
| `POST /v1/commands/send` | 完整提供身份和 `subscriptionId` 后返回 202，发送前先持久入队 |
| `GET /v1/commands/{commandId}` | 查询原命令，结果未知时不得重新发送 |
| `DELETE /v1/commands/{commandId}` | 取消尚未开始的命令，提前取消也保留标记 |
| `DELETE /v1/subscriptions/{subscriptionId}` | 停止生成新事件，保留未确认 outbox |

发送扩展中的 `sessionId`、`conversationId`、`subscriptionId` 缺少任意一项直接返回 422，不能忽略身份字段后进入旧发送入口。命令 ID 由主项目冻结的原工具调用提供；同 ID 同载荷执行一次，同 ID 不同载荷返回 409，新一轮相同文字必须有新命令 ID。

订阅请求包含 `schemaVersion:1`、`subscriptionId`、`consumerId`、`sessionId`、`conversationId`、`afterSequence`、`callbackUrl`、`callbackToken` 和单项 `eventTypes`。命令订阅的类型为 `command.completed`，另需 `commandId`；消息订阅的类型为 `message.received`。回调地址只能是 `http://127.0.0.1:<port>/api/local-runtime/qq/events`，密钥只在本机传输和数据库保存，不交给 LLM。

## 身份与消息证据

`identityKind:SERVICE_UI_EVIDENCE` 表示服务分配的持久 ID，不是 QQ 原生好友号。账号会话由进程、窗口和头像证据锚定，联系人由原始行名、头像及完整会话标题核验。读取和发送在原 GUI Dispatcher 的同一个任务内重新验证身份；发送前再次核验选中行和标题。相同行名和头像无法区分的行只列入 `identityWarnings`，不成为可操作联系人。

原生复制消息持久化后分配 `messageId` 和会话内递增序号。相同文字的独立消息不能按文本去重。复制文本没有可靠方向时，保留 `isSelf:null`、`direction:UNKNOWN`、`sender`、`timestamp`、`rawText` 和 `source:QQ_NATIVE_CLIPBOARD`，由主 Agent 判断是否商家回复。

采集仅覆盖当前可见聊天区域和先前采集入库的记录，不能获取 QQ 平台不可见的全量历史。消息 ID 由服务生成，不是原生消息 ID；完全相同的发送人、正文和时间且没有可区分界面证据时，不能承诺识别为两个独立消息。QQ 进程或账号头像证据改变后必须重新绑定。

观察器先回放已存历史，再采集新快照。服务重启、同一会话新订阅或采集后尚未写入 outbox 时崩溃，均可从持久历史继续投递。`conversation.invalidated` 沿消息订阅报告可证实的身份变化，保留 `reason`、`errorCode`，停止该订阅的新增观察；普通暂时截图失败不伪装身份失效。

## 持久化与确认

`qq-command-ledger.sqlite3` 中保存订阅、异步命令元数据、取消标记和 outbox。发送终态和完成事件在一个 SQLite 事务中提交。`QUEUED` 命令重启后可以执行；已经跨入 `RUNNING` 而没有结果的命令重启后记 `EFFECT_UNKNOWN`，只补回调，不重发。

每次回调带稳定 `eventId`，使用 `Authorization: Bearer <callbackToken>`。Runtime 只有在 Cloud inbox 已提交后才返回 `200 {"acknowledgedEventId":"原事件 ID"}`。服务同时校验状态码和事件 ID；超时、失败或确认不匹配只重投原事件，不重发 QQ 消息。已确认命令订阅退休；取消不会删除未确认 outbox。

取消返回 `RUNNING` 或 `EFFECT_UNKNOWN` 表示无法撤回或确认已经跨过执行边界的操作，不能向界面宣称消息已撤回。

## 验证范围

`tests/test_service_api.py` 验证旧 HTTP 契约，`tests/test_service_callbacks.py` 验证订阅、命令幂等、精确确认、取消、重启和身份失效。`tests/test_bound_identity.py` 验证持久身份、消息顺序和观察器。

将主项目 `client-runtime/python-runtime/src` 加入 `PYTHONPATH` 后，`tests/test_runtime_callback_integration.py` 运行两仓库实际 HTTP 路由、身份存储、观察器及 Runtime 工具的 ASGI 端到端测试。只有原始 QQ 操作和 Cloud 接收器使用替身，测试不访问真实商家会话。

真实 Windows QQ 的视觉证据稳定性和实际发送仍需在可控测试联系人中验收，不能由模拟端到端通过推断完成。
