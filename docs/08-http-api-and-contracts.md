# HTTP API 与契约

## 服务边界

FastAPI 只监听 `127.0.0.1` 的随机端口，endpoint 文件包含：

- `endpoint`
- `processId`
- `apiVersion`
- `logDirectory`
- `applicationLog`
- `consoleLog`
- `issuedAt`

当前服务不做 token 鉴权，调用方读取 endpoint 后直接请求接口。

## v1 接口

- `GET /v1/health`：健康检查。
- `POST /v1/capture:check`：检查 QQ 截图能力。
- `POST /v1/contacts:query`：读取联系人列表。请求体可选 `contactName`（先按名字滚动定位）
  与 `all`（缺省 `false` = 只读当前一屏，最快；`true` = 滚动扫掠整张名单）；
  响应为 `contacts` / `count` / `complete`，`complete=false` 表示这份名单没确认扫全。
- `POST /v1/commands/read`：读取指定联系人可见消息。
- `POST /v1/commands/send`：发送消息。
- `GET /v1/commands/{commandId}`：查询命令状态。
- `POST /v1/chat/history`：查询本地聊天记录。
- `POST /v1/qidian:wake`：接收应用已保存的真实 qidian 链接、公司信息、网站 QQ
  （`targetQqNumber`）和必填 `accountScope`。首次打开链接一次并轮询实际号码，完全匹配才保存。
  后续同范围、同 QQ 通过无障碍树重新定位激活，返回 `reused=true`；不再采用 120 秒同链缓存。
  激活、号码验证和落库均在单消费者队列内。业务失败 503，参数违规 422。
  本次新打开的企点页面明确提示目标 QQ 需先加好友时，返回 503 `QQ_QIDIAN_FRIEND_REQUIRED`，不保存映射；仅超时不返回此码。
- `POST /v1/qidian/mappings:query`：在必填 `accountScope` 内，按 `(siteId, supplierId)`
  或 `linkHash` 查询已验证映射；未命中返回 `found=false`。详见 `14-qidian-wake-and-mapping.md`。

## 契约模型

`service/contracts.py` 使用 Pydantic `StrictModel`：

- 未知字段拒绝。
- JSON 字段使用 camelCase 别名。
- 字段提供中文 `title`、`description` 和示例。
- 状态和方向使用 `Literal` 约束。

## 中文 OpenAPI

`service/app.py` 配置：

- 中文服务标题和说明。
- 接口 tag 分组。
- 每个接口的中文 summary/description。
- 请求字段和响应字段的中文 schema 文档。

Swagger UI 自身按钮由前端资源提供，仍可能是英文；API 内容和 schema 描述为中文。

## 客户端托管退出

客户端启动自有 EXE 时增加 `--shutdown-on-stdin`，并保持标准输入管道打开。
输入一行 `shutdown` 或管道 EOF 都会触发正常退出：停止接收请求，等待已进入的请求和 Dispatcher 任务结束，释放 WGC，再删除本进程发布的 endpoint。
独立启动不传该参数，不受此管道控制；没有新增 HTTP 关闭接口。

next 只关闭自己启动的子进程；复用已有服务时不关闭服务。正常退出最多等待 90 秒，超时才强制结束自有进程树，并记录 WGC 清理未确认的警告。
