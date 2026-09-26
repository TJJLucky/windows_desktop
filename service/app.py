"""QQ Desktop Service 的外部 HTTP 接口层。

本模块定义服务暴露给调用方（UniApp 前端 / 调试工具）的全部 HTTP 接口：
- 本机 loopback 服务不做 token 鉴权：所有接口均可直接访问（安全由"仅监听 127.0.0.1"保证）；
- 阻塞型 QQ 操作（视觉识别 + 模拟点击）由 FastAPI 对同步端点自动放入线程池执行，避免阻塞事件循环；
  （执行仍由内部 Dispatcher 单消费者队列串行，HTTP 线程只负责"入队 + 等待结果"）
- 发送命令走"命令账本"（command_ledger）保证幂等：相同 commandId 只执行一次；
- 聊天记录的读取与发送都会自动落库（chat_history），供后续查询。
"""

# 延迟求值类型注解：允许前向引用，加快 import 速度
from __future__ import annotations

import logging
import time
import uuid

# Path：数据库文件路径的类型标注
from pathlib import Path

# FastAPI 框架组件：FastAPI(应用对象) / HTTPException(标准错误响应) / ApiPath(路径参数文档)
from fastapi import FastAPI, HTTPException, Path as ApiPath

# ChatHistoryStore：聊天记录 SQLite 存储（追加消息、按联系人查历史）
from .chat_history import ChatHistoryStore
# CommandConflictError：相同 commandId 配不同消息体的冲突异常；CommandLedger：命令幂等账本
from .command_ledger import CommandConflictError, CommandLedger
# 全部 Pydantic 请求/响应契约模型（API 的输入输出 schema）
from .contracts import API_VERSION, CaptureCheckResponse, ChatHistoryRequest, ChatHistoryResponse, CommandResponse, ListContactsResponse, ReadMessagesRequest, ReadMessagesResponse, SendMessageRequest, ServiceHealthResponse
# QqAutomationPort：自动化端口抽象（测试时可注入替身）；LegacyQqAutomationFacade：真实实现（视觉 RPA）；QqAutomationError：QQ 操作失败统一异常
from .facade import LegacyQqAutomationFacade, QqAutomationError, QqAutomationPort

logger = logging.getLogger("qq_service.api")

API_DESCRIPTION = """QQ 桌面自动化服务。\n\n所有操作只监听本机 127.0.0.1，不提供公网访问；发送接口使用 commandId 保证幂等。\n"""

OPENAPI_TAGS = [
    {"name": "系统", "description": "服务状态与截图可用性检查。"},
    {"name": "联系人", "description": "读取 QQ 用户列表。"},
    {"name": "消息", "description": "读取和发送 QQ 消息。"},
    {"name": "聊天记录", "description": "本地聊天记录查询。"},
]


def create_app(
    automation: QqAutomationPort | None = None,
    *,
    ledger_path: Path,
    chat_history_path: Path | None = None,
) -> FastAPI:
    """构造 FastAPI 应用，注册全部 v1 接口并返回应用对象。

    参数：
    - automation：自动化实现（默认用真实视觉 RPA 门面；测试注入替身）；
    - ledger_path：命令账本数据库文件路径；
    - chat_history_path：聊天记录库路径（缺省自动放在账本同目录）。
    """

    # 聊天记录库未显式指定时，默认与账本放同一目录，便于统一管理
    chat_history_path = chat_history_path or (ledger_path.parent / "qq-chat-history.sqlite3")
    # 初始化聊天记录存储（打开 SQLite、建表、启用 WAL）
    chat_history = ChatHistoryStore(chat_history_path)
    # 自动化实现：外部传入了就用外部实现（测试/定制），否则用真实视觉 RPA 门面
    facade = automation or LegacyQqAutomationFacade()
    # 初始化命令账本（打开 SQLite、建表），用于发送命令的幂等控制
    ledger = CommandLedger(ledger_path)
    # 创建 FastAPI 应用：开启默认 /docs(Swagger) 与 /openapi.json，版本号取契约常量
    app = FastAPI(
        title="QQ 桌面自动化服务",
        version=API_VERSION,
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
    )
    logger.info(
        "application.create ledger_path=%s chat_history_path=%s automation=%s",
        ledger_path,
        chat_history_path,
        type(facade).__name__,
    )

    @app.middleware("http")
    async def log_request(request, call_next):
        """记录每个 HTTP 请求的 requestId、耗时、状态码和异常。"""
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        client = request.client.host if request.client else "-"
        logger.info(
            "http.request.start request_id=%s method=%s path=%s query=%r client=%s",
            request_id,
            request.method,
            request.url.path,
            request.url.query,
            client,
        )
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "http.request.error request_id=%s method=%s path=%s duration_ms=%.2f",
                request_id,
                request.method,
                request.url.path,
                (time.perf_counter() - started) * 1000,
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "http.request.end request_id=%s method=%s path=%s status=%d duration_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    # —— 健康检查接口 ——
    @app.get(
        "/v1/health",
        response_model=ServiceHealthResponse,
        summary="健康检查",
        description="检查服务是否已启动，用于客户端探测生命周期。",
        tags=["系统"],
        response_description="服务健康状态",
    )
    def health() -> ServiceHealthResponse:
        # 返回固定状态 READY + API 版本；调用方用它探测服务是否存活
        return ServiceHealthResponse(apiVersion=API_VERSION, status="READY")

    # —— 截图可用性检查接口（探测） ——
    @app.post(
        "/v1/capture:check",
        response_model=CaptureCheckResponse,
        summary="检查 QQ 截图可用性",
        description="检查 QQ 主窗口和 WGC 截图能力。窗口不可用是有效的 ready=false 检测结果，不返回 503。",
        tags=["系统"],
        response_description="截图可用性检测结果",
    )
    def check_capture() -> CaptureCheckResponse:
        # 同步端点：FastAPI 自动放入线程池执行窗口就绪检测（进程/窗口枚举 + 可能唤起 + WGC 试截）
        result = facade.check_capture_ready()
        logger.info(
            "capture.check ready=%s window_title=%r error=%s",
            result.get("ready"),
            result.get("windowTitle"),
            result.get("error"),
        )
        # 组装响应：ready 透传；windowTitle 就绪时才有；error 不可用时才有
        # 注意：窗口不可用返回 200 + ready=false（探测语义），不投影为 503
        return CaptureCheckResponse(
            ready=result["ready"],
            method="ensure_qq_window_with_retry",
            windowTitle=result.get("windowTitle"),
            error=result.get("error"),
        )

    # —— 读取联系人列表接口 ——
    @app.post(
        "/v1/contacts:query",
        response_model=ListContactsResponse,
        summary="查询 QQ 联系人列表",
        description="读取当前 QQ 用户列表。返回的 contacts key 可原样用于消息读取、发送和聊天记录查询。",
        tags=["联系人"],
        response_description="联系人列表和数量",
    )
    def list_contacts() -> ListContactsResponse:
        try:
            # 同步端点：FastAPI 自动在（线程池）执行 QQ 视觉识别，事件循环不被阻塞
            contacts = facade.list_contacts()
            logger.info("contacts.query.success count=%d", len(contacts))
        except QqAutomationError as exc:
            logger.exception("contacts.query.error")
            # QQ 操作失败（窗口不可用、识别失败等）统一返回 503，并把内部原因带给调用方
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # 组装响应：联系人数组 + 数量
        return ListContactsResponse(contacts=contacts, count=len(contacts))

    # —— 读取指定联系人的可见消息接口 ——
    @app.post(
        "/v1/commands/read",
        response_model=ReadMessagesResponse,
        summary="读取指定联系人的可见消息",
        description="激活联系人并读取当前可见消息窗口，成功后自动写入本地聊天记录。",
        tags=["消息"],
        response_description="当前可见消息列表",
    )
    def read_messages(request: ReadMessagesRequest) -> ReadMessagesResponse:
        try:
            # 同步端点：FastAPI 自动在（线程池）执行视觉读取消息区 + OCR 识别文本
            payload = facade.read_messages(request.contact_name)
            logger.info(
                "messages.read.success contact=%s count=%d",
                request.contact_name,
                len(payload.get("messages", [])),
            )
        except QqAutomationError as exc:
            logger.exception("messages.read.error contact=%s", request.contact_name)
            # QQ 操作失败 → 503（不会把读取结果落库，因为根本没读到）
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # 读到的可见消息自动落库（供聊天记录查询），失败仅告警不阻断
        _record_payload(chat_history, request.contact_name, payload)
        # 校验并返回读取结果（payload 是 dict，用模型校验后返回，保证 schema 一致性）
        return ReadMessagesResponse.model_validate(payload)

    # —— 发送消息接口（幂等） ——
    @app.post(
        "/v1/commands/send",
        response_model=CommandResponse,
        summary="发送消息",
        description="向指定联系人发送消息。相同 commandId 和相同消息体只执行一次，相同 ID 不同消息体返回 409。",
        tags=["消息"],
        responses={409: {"description": "commandId 被不同消息体重复使用"}},
        response_description="发送命令状态",
    )
    def send_message(request: SendMessageRequest) -> CommandResponse:
        logger.info(
            "message.send.begin command_id=%s contact=%s text_length=%d",
            request.command_id,
            request.contact_name,
            len(request.text),
        )
        try:
            # 先向账本申请执行：begin 返回 (是否真正执行, 当前状态, 已有结果)。
            # 相同 commandId 相同消息体 → 幂等返回已有结果不重发；相同 ID 不同消息体 → 抛 409
            execute, current_status, result = ledger.begin(
                request.command_id, request.contact_name, request.text
            )
        except CommandConflictError as exc:
            logger.warning("message.send.conflict command_id=%s", request.command_id)
            # 冲突（同一 commandId 配不同消息体）→ 409，明确告知调用方
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not execute:
            logger.info(
                "message.send.idempotent command_id=%s status=%s",
                request.command_id,
                current_status,
            )
            # 账本判定无需再次执行（已在跑或已完成）：直接把当前状态与结果原样返回
            return CommandResponse(commandId=request.command_id, status=current_status, result=result)
        try:
            # 真正执行发送：FastAPI 线程池里跑视觉定位输入框 + 粘贴 + 快捷键发送
            result = facade.send_message(request.contact_name, request.text)
        except QqAutomationError as exc:
            logger.exception("message.send.failed command_id=%s", request.command_id)
            # 发送失败：构造失败结果并让账本落 FAILED 终态（不会留下悬空的 RUNNING）
            result = {"ok": False, "sent": False, "textLength": len(request.text), "error": str(exc)}
            ledger.resolve(request.command_id, "FAILED", result)
            # 返回 FAILED 状态给调用方
            return CommandResponse(commandId=request.command_id, status="FAILED", result=result)
        # 发送成功：账本落 SUCCEEDED 终态
        ledger.resolve(request.command_id, "SUCCEEDED", result)
        # 发出的消息自动落库为 out（我方发出），关联 commandId 便于追溯；失败仅告警
        _try_mark_outbound(chat_history, request.contact_name, request.text, request.command_id)
        logger.info("message.send.success command_id=%s", request.command_id)
        # 返回 SUCCEEDED 状态 + 实际执行结果
        return CommandResponse(commandId=request.command_id, status="SUCCEEDED", result=result)

    # —— 查询发送命令状态接口 ——
    @app.get(
        "/v1/commands/{command_id}",
        response_model=CommandResponse,
        summary="查询命令状态",
        description="根据 commandId 查询发送命令的执行状态和结果。",
        tags=["消息"],
        responses={404: {"description": "commandId 不存在"}},
        response_description="命令当前状态",
    )
    def command_status(
        command_id: str = ApiPath(description="发送接口使用的 commandId。", examples=["effect-key-0000001"]),
    ) -> CommandResponse:
        # 从账本按 commandId 查状态与结果（SQLite 毫秒级，同步直调）
        record = ledger.get(command_id)
        if record is None:
            logger.warning("command.status.not_found command_id=%s", command_id)
            # 账本中不存在该 commandId → 404，防止调用方把未知 ID 当成功
            raise HTTPException(status_code=404, detail="QQ_COMMAND_NOT_FOUND")
        # 解包 (当前状态, 结果) 并返回
        current_status, result = record
        logger.info("command.status command_id=%s status=%s", command_id, current_status)
        return CommandResponse(commandId=command_id, status=current_status, result=result)

    # —— 查询聊天记录接口（查询前自动视觉更新一次） ——
    @app.post(
        "/v1/chat/history",
        response_model=ChatHistoryResponse,
        summary="查询聊天记录",
        description="查询本地聊天记录。查询前会先做一次视觉更新；更新失败时返回旧数据并标记 update=failed。",
        tags=["聊天记录"],
        response_description="联系人聊天记录",
    )
    def query_chat_history(request: ChatHistoryRequest) -> ChatHistoryResponse:
        # 需求约定：每次查询先做一次视觉读取（拉到最新消息），再返回历史，保证数据新鲜
        try:
            payload = facade.read_messages(request.contact_name)
        except QqAutomationError:
            # 视觉读取失败（如窗口不可用）：不阻断历史查询，只把更新标记为 failed 告知调用方
            logger.warning("chat.history.update_failed contact=%s", request.contact_name)
            update = "failed"  # 自动更新失败不阻断历史查询
        else:
            # 读取成功：把新消息落库，更新标记为 ok
            _record_payload(chat_history, request.contact_name, payload)
            logger.info(
                "chat.history.update_ok contact=%s visible_count=%d",
                request.contact_name,
                len(payload.get("messages", [])),
            )
            update = "ok"
        # 从存储中取出该联系人的历史消息（按 seq 升序）
        messages = chat_history.get_history(request.contact_name)
        logger.info("chat.history.query contact=%s update=%s count=%d", request.contact_name, update, len(messages))
        # 返回联系人名、消息条数、消息列表与本次更新状态
        return ChatHistoryResponse(
            contactName=request.contact_name, count=len(messages), messages=messages, update=update
        )

    # 返回构造好的应用对象（由调用方交给 uvicorn 托管）
    return app


def _record_payload(chat_history: ChatHistoryStore, contact_name: str, payload: dict) -> None:
    """把一次视觉读取结果作为可见窗口差异写入聊天记录存储。"""
    try:
        inserted = chat_history.append_visible(contact_name, payload["messages"])
        logger.info("chat.history.append contact=%s inserted=%d", contact_name, inserted)
    except Exception:
        logger.exception("chat.history.append_failed contact=%s", contact_name)


def _try_mark_outbound(
    chat_history: ChatHistoryStore,
    contact_name: str,
    text: str,
    command_id: str,
) -> None:
    """登记已发送消息；写入失败仅打印告警，不阻断主流程。"""
    try:
        chat_history.mark_outbound(contact_name, text, command_id)
        logger.info("chat.history.mark_outbound contact=%s command_id=%s", contact_name, command_id)
    except Exception:
        logger.exception("chat.history.mark_outbound_failed contact=%s command_id=%s", contact_name, command_id)
