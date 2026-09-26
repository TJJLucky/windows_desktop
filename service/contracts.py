"""本机 QQ Desktop Service v1 HTTP 契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


API_VERSION = "v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceHealthResponse(StrictModel):
    api_version: Literal["v1"] = Field(alias="apiVersion")
    status: Literal["READY"]


class ListContactsResponse(StrictModel):
    contacts: dict[str, dict[str, Any]]
    count: int = Field(ge=0)


class ReadMessagesRequest(StrictModel):
    contact_name: str = Field(alias="contactName", min_length=1, max_length=256)


class ReadMessagesResponse(StrictModel):
    ok: bool
    messages: list[dict[str, Any]]
    count: int = Field(ge=0)
    error: str | None = None


class SendMessageRequest(StrictModel):
    command_id: str = Field(alias="commandId", min_length=16, max_length=128)
    contact_name: str = Field(alias="contactName", min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=5000)


class CommandResponse(StrictModel):
    command_id: str = Field(alias="commandId")
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "EFFECT_UNKNOWN"]
    result: dict[str, Any] | None = None


class ChatHistoryItem(StrictModel):
    id: int
    direction: Literal["in", "out"]
    text: str
    seq: int
    command_id: str | None = Field(alias="commandId", default=None)
    created_at: str = Field(alias="createdAt")


class ChatHistoryRequest(StrictModel):
    contact_name: str = Field(alias="contactName", min_length=1, max_length=256)


class ChatHistoryResponse(StrictModel):
    contact_name: str = Field(alias="contactName")
    count: int = Field(ge=0)
    messages: list[ChatHistoryItem]
    update: Literal["ok", "failed"]  # 本次查询前自动更新的结果
