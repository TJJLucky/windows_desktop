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
- `POST /v1/qidian:wake`：接收 qidian 链接、公司信息与**调用方从华强搜索结果页读到的商家 QQ 号**
  （`targetQqNumber`，必填；服务端不解析链接里的号）、串行唤醒 QQ、
  等会话稳定后识别当前接待人，并按 `(siteId, supplierId)` 覆盖写入映射。同一链接 120 秒内
  重复提交命中 `reused=true`（不重复唤醒）。失败投影 503（`QQ_QIDIAN_*`），链接违规投影 422。
- `POST /v1/qidian/mappings:query`：按 `(siteId, supplierId)` 或 `linkHash` 读回 qidian 唤醒映射；
  没有记录返回 `found=false`。详见 `14-qidian-wake-and-mapping.md`。

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