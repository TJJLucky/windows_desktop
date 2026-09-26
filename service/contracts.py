"""本机 QQ Desktop Service v1 HTTP 契约。

定义全部请求/响应模型（Pydantic）：
- 字段使用 JSON 别名（如 contactName），与调用方（UniApp 前端）的命名约定一致；
- 全部模型继承 StrictModel：未知字段直接报错（extra="forbid"），保证前后端契约严格一致，防止前端拼错字段名被静默忽略；
- 枚举用 Literal 约束，非法取值在序列化/校验阶段就失败。
"""

# 延迟求值类型注解：允许前向引用、加快 import
from __future__ import annotations

# Any：宽松类型（嵌套结构用 dict 承载）；Literal：把字段取值限定为字面量集合
from typing import Any, Literal

# Pydantic v2：BaseModel(模型基类) / ConfigDict(模型配置) / Field(字段级约束与别名)
from pydantic import BaseModel, ConfigDict, Field


# 服务 API 版本号：与 endpoint 文件里的 apiVersion 一致
API_VERSION = "v1"


class StrictModel(BaseModel):
    """所有契约模型的基类：拒绝未知字段，前后端字段必须严格对齐。"""
    # extra="forbid"：请求/响应中出现未声明字段时直接校验失败（而非默默丢弃）
    model_config = ConfigDict(extra="forbid")


class ServiceHealthResponse(StrictModel):
    """健康检查响应：API 版本 + 固定 READY 状态。"""
    # 别名 apiVersion：对外 JSON 键是 apiVersion，内部字段名用蛇形 api_version
    api_version: Literal["v1"] = Field(alias="apiVersion")
    # 状态只允许 READY（Literal 限定）
    status: Literal["READY"]


class ListContactsResponse(StrictModel):
    """联系人列表响应：联系人名 → 联系人信息 dict 的映射。"""
    # 字典结构：键为联系人名，值为任意属性（头像/备注等），保持灵活
    contacts: dict[str, dict[str, Any]]
    # 联系人总数（不允许负数）
    count: int = Field(ge=0)


class ReadMessagesRequest(StrictModel):
    """读取消息请求：指定联系人名。"""
    # 联系人名：非空（min_length=1）、最长 256；别名 contactName
    contact_name: str = Field(alias="contactName", min_length=1, max_length=256)


class ReadMessagesResponse(StrictModel):
    """读取消息响应：成功标志 + 消息数组 + 条数 + 可选错误信息。"""
    # 视觉读取是否成功
    ok: bool
    # 消息数组：每条含文本/坐标/方向等（与 Message 模型序列化一致）
    messages: list[dict[str, Any]]
    # 消息条数（不允许负数）
    count: int = Field(ge=0)
    # 失败时的错误信息（成功时为 null）
    error: str | None = None


class SendMessageRequest(StrictModel):
    """发送消息请求：幂等键 + 目标联系人 + 消息文本。"""
    # 调用方生成的稳定幂等键：至少 16 字符、最长 128；账本据此去重与防冲突
    command_id: str = Field(alias="commandId", min_length=16, max_length=128)
    # 目标联系人名：非空、最长 256
    contact_name: str = Field(alias="contactName", min_length=1, max_length=256)
    # 消息文本：非空、最长 5000 字符
    text: str = Field(min_length=1, max_length=5000)


class CommandResponse(StrictModel):
    """命令响应：幂等键 + 执行状态 + 结果。"""
    # 幂等键（与请求一致）
    command_id: str = Field(alias="commandId")
    # 状态机取值：RUNNING(执行中) / SUCCEEDED(成功) / FAILED(失败) / EFFECT_UNKNOWN(重启后结果不确定)
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "EFFECT_UNKNOWN"]
    # 执行结果细节（发送成功时含 sent/textLength；失败时含 error）
    result: dict[str, Any] | None = None


class CaptureCheckResponse(StrictModel):
    """截图可用性检查响应：QQ 窗口就绪检测结果。"""
    # 截图是否可用（窗口就绪 = True；不可用 = False，属于有效检测结果而非错误）
    ready: bool
    # 本次检测使用的方法（固定为 ensure_qq_window_with_retry，便于调用方排查）
    method: Literal["ensure_qq_window_with_retry"]
    # 就绪时的 QQ 主窗口标题（辅助确认窗口身份；不可用时为 null）
    window_title: str | None = Field(alias="windowTitle", default=None)
    # 不可用时的错误码（如 QQ_WINDOW_NOT_READY；就绪时为 null）
    error: str | None = None


class ChatHistoryItem(StrictModel):
    """单条聊天记录（历史查询返回元素）。"""
    # 数据库自增主键
    id: int
    # 消息方向：in(对方发来) / out(我方发出)
    direction: Literal["in", "out"]
    # 消息文本
    text: str
    # 该联系人维度下的消息序号（按 seq 排序即时间顺序）
    seq: int
    # 若是我方经命令发送的消息，关联其 commandId（便于追溯）
    command_id: str | None = Field(alias="commandId", default=None)
    # 入库时间（ISO 时间戳）
    created_at: str = Field(alias="createdAt")


class ChatHistoryRequest(StrictModel):
    """查询聊天记录请求：指定联系人。"""
    # 联系人名：非空、最长 256
    contact_name: str = Field(alias="contactName", min_length=1, max_length=256)


class ChatHistoryResponse(StrictModel):
    """查询聊天记录响应：联系人 + 条数 + 消息列表 + 本次自动更新结果。"""
    # 联系人名
    contact_name: str = Field(alias="contactName")
    # 返回的消息条数（不允许负数）
    count: int = Field(ge=0)
    # 历史消息列表（按 seq 升序）
    messages: list[ChatHistoryItem]
    # 本次查询前的自动视觉更新是否成功（failed 表示读到旧数据）
    update: Literal["ok", "failed"]  # 本次查询前自动更新的结果
