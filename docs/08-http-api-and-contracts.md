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
- `POST /v1/contacts:query`：读取联系人列表。
- `POST /v1/commands/read`：读取指定联系人可见消息。
- `POST /v1/commands/send`：发送消息。
- `GET /v1/commands/{commandId}`：查询命令状态。
- `POST /v1/chat/history`：查询本地聊天记录。

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