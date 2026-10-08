"""本机 QQ Desktop Service v1 HTTP 契约。

定义全部请求/响应模型（Pydantic）：
- 字段使用 JSON 别名（如 contactName），与调用方（UniApp 前端）的命名约定一致；
- 全部模型继承 StrictModel：未知字段直接报错（extra="forbid"），保证前后端契约严格一致；
- 字段提供中文 title/description/examples，作为 FastAPI OpenAPI 文档。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from urllib.parse import urlparse


API_VERSION = "v1"


class StrictModel(BaseModel):
    """所有契约模型的基类：拒绝未知字段，前后端字段必须严格对齐。"""

    model_config = ConfigDict(extra="forbid")


class ServiceHealthResponse(StrictModel):
    """健康检查响应。"""

    api_version: Literal["v1"] = Field(
        alias="apiVersion",
        title="API 版本",
        description="当前 API 版本，固定为 v1。",
        examples=["v1"],
    )
    status: Literal["READY"] = Field(
        title="服务状态",
        description="服务状态，固定为 READY。",
        examples=["READY"],
    )


class ListContactsResponse(StrictModel):
    """联系人列表响应。"""

    contacts: dict[str, dict[str, Any]] = Field(
        title="联系人列表",
        description="联系人名到联系人信息的映射，key 来自 QQ 用户列表。",
    )
    count: int = Field(
        ge=0,
        title="联系人数量",
        description="本次返回的联系人数量。",
    )


class ReadMessagesRequest(StrictModel):
    """读取指定联系人可见消息的请求。"""

    contact_name: str = Field(
        alias="contactName",
        min_length=1,
        max_length=256,
        title="联系人名",
        description="QQ 用户列表中的联系人名，必须使用 /v1/contacts:query 返回的 key。",
        examples=["华强电子"],
    )
    session_id: str | None = Field(default=None, alias="sessionId", min_length=1, max_length=160)
    conversation_id: str | None = Field(default=None, alias="conversationId", min_length=1, max_length=160)

    @model_validator(mode="after")
    def require_stable_identity(self):
        if bool(self.session_id) != bool(self.conversation_id):
            raise ValueError("sessionId 与 conversationId 必须同时提供")
        return self


class ReadMessagesResponse(StrictModel):
    """读取当前可见消息的响应（来源为 QQ 原生复制文本）。"""

    ok: bool = Field(title="读取是否成功", description="QQ 框选复制与文本解析是否成功。")
    messages: list[dict[str, Any]] = Field(
        title="消息列表",
        description="当前可见消息列表，包含 sender、timestamp、text 和 rawText。",
    )
    count: int = Field(ge=0, title="消息数量", description="本次返回的消息数量。")
    copied_text: str | None = Field(
        alias="copiedText",
        default=None,
        title="原始复制文本",
        description="本次读取使用的完整 QQ 剪贴板文本。",
    )
    error: str | None = Field(
        default=None,
        title="错误信息",
        description="失败时的错误信息，成功时为 null。",
    )


class SendMessageRequest(StrictModel):
    """发送消息请求。"""

    command_id: str = Field(
        alias="commandId",
        min_length=16,
        max_length=128,
        title="命令 ID",
        description="调用方生成的稳定幂等键，同一 ID 不得用于不同消息体。",
        examples=["effect-key-0000001"],
    )
    contact_name: str = Field(
        alias="contactName",
        min_length=1,
        max_length=256,
        title="联系人名",
        description="QQ 用户列表中的联系人名，必须使用 /v1/contacts:query 返回的 key。",
        examples=["华强电子"],
    )
    text: str = Field(
        min_length=1,
        max_length=5000,
        title="消息文本",
        description="要发送的消息文本，最长 5000 个字符。",
        examples=["STM32F103C8T6，数量 100，请报价。"],
    )
    session_id: str | None = Field(default=None, alias="sessionId", min_length=1, max_length=160)
    conversation_id: str | None = Field(default=None, alias="conversationId", min_length=1, max_length=160)
    subscription_id: str | None = Field(default=None, alias="subscriptionId", min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_callback_identity(self):
        identity = (self.session_id, self.conversation_id, self.subscription_id)
        if any(identity) and not all(identity):
            raise ValueError("异步发送必须同时提供 sessionId、conversationId 与 subscriptionId")
        return self


class subscription_request(StrictModel):
    """持久回调订阅。回调密钥只用于本机传输，不进入日志或自动化消息。"""

    schema_version: Literal[1] = Field(alias="schemaVersion")
    subscription_id: str = Field(alias="subscriptionId", min_length=1, max_length=128)
    consumer_id: str = Field(alias="consumerId", min_length=1, max_length=128)
    callback_url: str = Field(alias="callbackUrl", max_length=512)
    callback_token: str = Field(alias="callbackToken", min_length=32, max_length=128, repr=False)
    event_types: list[Literal["message.received", "command.completed"]] = Field(alias="eventTypes", min_length=1, max_length=1)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=160)
    conversation_id: str = Field(alias="conversationId", min_length=1, max_length=160)
    after_sequence: int = Field(alias="afterSequence", ge=0, strict=True)
    command_id: str | None = Field(default=None, alias="commandId", min_length=16, max_length=128)

    @field_validator("callback_url")
    @classmethod
    def require_loopback_callback(cls, value):
        parsed = urlparse(value)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None
                or parsed.path != "/api/local-runtime/qq/events" or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError("回调只能投递到 Runtime 的本机端点")
        return value

    @model_validator(mode="after")
    def require_command_filter(self):
        if (self.event_types == ["command.completed"]) != bool(self.command_id):
            raise ValueError("命令订阅必须指定 commandId，消息订阅不接受 commandId")
        return self


class consumer_request(StrictModel):
    """退订与排队命令取消的消费方标识。"""

    consumer_id: str = Field(alias="consumerId", min_length=1, max_length=128)

class CommandResponse(StrictModel):
    """发送命令状态响应。"""

    command_id: str = Field(
        alias="commandId",
        title="命令 ID",
        description="发送请求的幂等键。",
    )
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "EFFECT_UNKNOWN"] = Field(
        title="命令状态",
        description="RUNNING：执行中；SUCCEEDED：成功；FAILED：失败；EFFECT_UNKNOWN：结果未知，禁止自动重发。",
    )
    result: dict[str, Any] | None = Field(
        default=None,
        title="执行结果",
        description="发送成功时包含 sent/textLength，失败时包含 error。",
    )


class CaptureCheckResponse(StrictModel):
    """截图可用性检查响应。"""

    ready: bool = Field(title="截图是否就绪", description="QQ 窗口和 WGC 截图是否可用。")
    method: Literal["ensure_qq_window_with_retry"] = Field(
        title="检测方法",
        description="本次截图可用性检测使用的方法。",
    )
    window_title: str | None = Field(
        alias="windowTitle",
        default=None,
        title="窗口标题",
        description="就绪时的 QQ 主窗口标题，不可用时为 null。",
    )
    error: str | None = Field(
        default=None,
        title="错误码",
        description="不可用时的错误码，就绪时为 null。",
        examples=["QQ_WINDOW_NOT_READY"],
    )


class ChatHistoryItem(StrictModel):
    """单条聊天记录。"""

    id: int = Field(title="记录 ID", description="聊天记录数据库中的自增主键。")
    sender: str | None = Field(default=None, title="发送人", description="QQ 复制文本中的发送人。")
    timestamp: str | None = Field(default=None, title="消息时间", description="QQ 复制文本中的月日时分秒。")
    text: str = Field(title="消息文本", description="QQ 复制文本中的消息正文。")
    raw_text: str | None = Field(
        alias="rawText",
        default=None,
        title="原始消息文本",
        description="从剪贴板中截取的完整单条消息，包含发送人、时间和正文。",
    )
    seq: int = Field(title="消息序号", description="同一联系人维度下按时间顺序递增。")
    created_at: str = Field(alias="createdAt", title="记录时间", description="消息入库时间。")


class ChatHistoryRequest(StrictModel):
    """查询聊天记录的请求。"""

    contact_name: str = Field(
        alias="contactName",
        min_length=1,
        max_length=256,
        title="联系人名",
        description="QQ 用户列表中的联系人名，必须使用 /v1/contacts:query 返回的 key。",
        examples=["华强电子"],
    )


class ChatHistoryResponse(StrictModel):
    """查询聊天记录的响应。"""

    contact_name: str = Field(
        alias="contactName",
        title="联系人名",
        description="聊天记录所属的 QQ 用户列表联系人名。",
    )
    count: int = Field(ge=0, title="消息数量", description="本次返回的历史消息数量。")
    copied_text: str | None = Field(
        alias="copiedText",
        default=None,
        title="本次原始复制文本",
        description="本次更新成功时使用的完整 QQ 剪贴板文本；更新失败时为 null。",
    )
    messages: list[ChatHistoryItem] = Field(
        title="历史消息",
        description="按 seq 升序排列的历史消息列表。",
    )
    update: Literal["ok", "failed"] = Field(
        title="自动更新结果",
        description="ok：查询前 QQ 复制更新成功；failed：复制或解析失败，返回旧数据。",
    )
