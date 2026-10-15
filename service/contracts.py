"""本机 QQ Desktop Service v1 HTTP 契约。

定义全部请求/响应模型（Pydantic）：
- 字段使用 JSON 别名（如 contactName），与调用方（UniApp 前端）的命名约定一致；
- 全部模型继承 StrictModel：未知字段直接报错（extra="forbid"），保证前后端契约严格一致；
- 字段提供中文 title/description/examples，作为 FastAPI OpenAPI 文档。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class ListContactsRequest(StrictModel):
    """不指定目标时读当前屏幕；指定目标且不可见时滚动查找。"""

    contact_name: str | None = Field(
        alias="contactName", default=None, min_length=1, max_length=256,
        title="待查找联系人", description="可选的已确认联系人名；无障碍树当前屏幕找不到时才滚动查找。",
        examples=["DIsaac"],
    )
    all: bool = Field(
        alias="all", default=False,
        title="是否返回全部联系人",
        description="true = 滚动扫掠整张 QQ 会话名单（不只当前一屏）；false（默认）= 只读当前一屏，最快。",
    )


class ListContactsResponse(StrictModel):
    """联系人列表响应。"""

    contacts: dict[str, dict[str, Any]] = Field(
        title="联系人列表",
        description="联系人名到联系人信息的映射，key 来自 QQ 会话列表（无障碍树行名）；"
                    "每行的 unread/unreadCount 来自 QQ 未读角标（unreadCount 取不到时为 null）。",
    )
    count: int = Field(
        ge=0,
        title="联系人数量",
        description="本次返回的联系人数量。",
    )
    complete: bool = Field(
        title="是否为完整名单",
        description="true = 本次返回覆盖了整张会话名单；false = 只覆盖当前一屏，"
                    "或全量扫掠未能确认扫全（列表容器能力不足 / 扫掠自检发现跳段）。",
    )


class ReadMessagesRequest(StrictModel):
    """读取指定联系人可见消息的请求。"""

    contact_name: str = Field(
        alias="contactName",
        min_length=1,
        max_length=256,
        title="联系人名",
        description="QQ 会话列表里的联系人名，必须使用 /v1/contacts:query 返回的 key。",
        examples=["DIsaac"],
    )


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
        description="QQ 会话列表里的联系人名，必须使用 /v1/contacts:query 返回的 key。",
        examples=["DIsaac"],
    )
    text: str = Field(
        min_length=1,
        max_length=5000,
        title="消息文本",
        description="要发送的消息文本，最长 5000 个字符。",
        examples=["STM32F103C8T6，数量 100，请报价。"],
    )

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
        description="QQ 会话列表里的联系人名，必须使用 /v1/contacts:query 返回的 key。",
        examples=["DIsaac"],
    )


class ChatHistoryResponse(StrictModel):
    """查询聊天记录的响应。"""

    contact_name: str = Field(
        alias="contactName",
        title="联系人名",
        description="聊天记录所属的会话名。",
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


class IdentifyCurrentContactRequest(StrictModel):
    """仅识别当前激活行，无外部操作参数。"""


class IdentifyCurrentContactResponse(StrictModel):
    skipped: bool = False
    reason: str | None = None
    qq_number: str | None = Field(alias="qqNumber", pattern=r"^[1-9][0-9]{4,19}$", title="QQ 号")
class QidianWakeRequest(StrictModel):
    """qidian 唤醒请求：链接 + 该链接对应的公司信息（从网站上提取）。"""

    link: str = Field(
        min_length=8,
        max_length=4096,
        title="qidian 链接",
        description="华强电子网 qidian 联系链接，只接受 http(s)://qidianqq.hqew.com/GoClient"
                    "（拒 userinfo、端口、其他路径、子域变体与其他协议）。",
        examples=["https://qidianqq.hqew.com/GoClient?QQ=123456789&Signature=abc"],
    )
    site_id: str = Field(
        alias="siteId",
        min_length=1,
        max_length=64,
        title="站点 ID",
        description="链接所属站点标识（与客户端 SiteDescriptor.id 对齐，如 hqew）。",
        examples=["hqew"],
    )
    supplier_id: str = Field(
        alias="supplierId",
        min_length=1,
        max_length=128,
        title="供应商 ID",
        description="站内供应商标识；与 siteId 一起构成映射主键。",
        examples=["hqew-8899"],
    )
    supplier_name: str = Field(
        alias="supplierName",
        min_length=1,
        max_length=256,
        title="供应商名称",
        description="从网站上提取的公司名，仅用于审计与人工核对。",
        examples=["深圳市某某电子有限公司"],
    )
    target_id: str | None = Field(
        alias="targetId",
        default=None,
        max_length=128,
        title="询价目标 ID",
        description="可选的询价目标标识，仅用于审计追溯，不参与映射。",
    )
    mpn: str | None = Field(
        default=None,
        max_length=128,
        title="物料型号",
        description="可选的目标型号（如 STM32F103C8T6），仅用于审计追溯。",
    )
    batch_id: str | None = Field(
        alias="batchId",
        default=None,
        max_length=128,
        title="批次 ID",
        description="可选的批次标识，仅用于审计追溯。",
    )

    @field_validator("link", "site_id", "supplier_id", "supplier_name")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """必填文本去首尾空白；全空白按参数违规拒绝。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能是空白字符串")
        return stripped


class QidianWakeResponse(StrictModel):
    """qidian 唤醒结果：本次唤醒识别到的接待人及其 QQ 号。"""

    link_hash: str = Field(
        alias="linkHash",
        title="链接指纹",
        description="qidian 链接的 sha256 指纹，用于同链去重与映射查询。",
    )
    site_id: str = Field(alias="siteId", title="站点 ID", description="本次请求的站点标识。")
    supplier_id: str = Field(alias="supplierId", title="供应商 ID", description="本次请求的供应商标识。")
    supplier_name: str = Field(alias="supplierName", title="供应商名称", description="本次请求携带的供应商名称。")
    contact_name: str = Field(
        alias="contactName",
        title="接待人会话名",
        description="QQ 会话列表里被激活的那一行名字（唤醒后由无障碍树读出）。",
    )
    qq_number: str | None = Field(
        alias="qqNumber",
        default=None,
        pattern=r"^[1-9][0-9]{4,19}$",
        title="接待人 QQ 号",
        description="识别到的当前接待人 QQ 号；群会话或不提供复制项的会话为 null。",
    )
    target_qq_number: str | None = Field(
        alias="targetQqNumber",
        default=None,
        pattern=r"^[1-9][0-9]{4,19}$",
        title="链接目标 QQ 号",
        description="从 qidian 链接解析出的目标 QQ 号；解析不到属于业务失败，不会返回该字段。",
    )
    qq_number_verified: bool = Field(
        alias="qqNumberVerified",
        title="QQ 号是否一致",
        description="true = 识别到的接待人 QQ 号与链接解析出的目标 QQ 号完全一致。",
    )
    active_stable: bool = Field(
        alias="activeStable",
        title="激活行是否稳定",
        description="true = 已等到连续两次同一份会话名单且只有唯一激活行。",
    )
    reused: bool = Field(
        title="是否复用",
        description="true = 同一链接在去重窗口内命中上次结果，本次没有再次唤醒 QQ。",
    )
    woke_at: str = Field(
        alias="wokeAt",
        title="唤醒时间",
        description="本次（或命中的上次）唤醒完成时间，ISO 8601 带时区偏移。",
    )


class QidianMappingQuery(StrictModel):
    """按 (siteId, supplierId) 或 linkHash 读回 qidian 唤醒映射。"""

    site_id: str | None = Field(
        alias="siteId",
        default=None,
        max_length=64,
        title="站点 ID",
        description="与 supplierId 一起定位映射；未提供 linkHash 时必填。",
    )
    supplier_id: str | None = Field(
        alias="supplierId",
        default=None,
        max_length=128,
        title="供应商 ID",
        description="与 siteId 一起定位映射；未提供 linkHash 时必填。",
    )
    link_hash: str | None = Field(
        alias="linkHash",
        default=None,
        max_length=128,
        title="链接指纹",
        description="按链接指纹反查映射；与 (siteId, supplierId) 二选一。",
    )

    @model_validator(mode="after")
    def _require_lookup_key(self) -> "QidianMappingQuery":
        """必须给出 linkHash，或同时给出 siteId 与 supplierId。"""
        if self.link_hash:
            self.link_hash = self.link_hash.strip()
        if self.link_hash or (self.site_id and self.supplier_id):
            return self
        raise ValueError("必须提供 linkHash，或同时提供 siteId 与 supplierId")


class QidianMappingResponse(StrictModel):
    """qidian 唤醒映射查询结果；found=false 时其余字段均为 null。"""

    found: bool = Field(title="是否命中映射", description="false = 该键还没有映射记录，其余字段均为 null。")
    site_id: str | None = Field(alias="siteId", default=None, title="站点 ID", description="映射主键的一部分。")
    supplier_id: str | None = Field(alias="supplierId", default=None, title="供应商 ID", description="映射主键的一部分。")
    link_hash: str | None = Field(alias="linkHash", default=None, title="链接指纹", description="最近一次唤醒使用的链接指纹。")
    supplier_name: str | None = Field(alias="supplierName", default=None, title="供应商名称", description="最近一次唤醒携带的公司名。")
    contact_name: str | None = Field(alias="contactName", default=None, title="接待人会话名", description="最近一次唤醒识别到的 QQ 会话名。")
    qq_number: str | None = Field(alias="qqNumber", default=None, title="接待人 QQ 号", description="最近一次识别到的接待人 QQ 号；不提供复制项的会话为 null。")
    target_qq_number: str | None = Field(alias="targetQqNumber", default=None, title="链接目标 QQ 号", description="最近一次从链接解析出的目标 QQ 号。")
    source: Literal["qidian_wake"] | None = Field(default=None, title="映射来源", description="映射写入来源，当前只有 qidian_wake。")
    updated_at: str | None = Field(alias="updatedAt", default=None, title="更新时间", description="最近一次覆盖写入时间，ISO 8601 带时区偏移。")
    wake_count: int | None = Field(alias="wakeCount", default=None, ge=0, title="唤醒次数", description="该映射键累计写入次数。")
