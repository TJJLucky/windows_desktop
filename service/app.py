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
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import time
import uuid

# Path：数据库文件路径的类型标注
from pathlib import Path

# FastAPI 框架组件：FastAPI(应用对象) / HTTPException(标准错误响应) / ApiPath(路径参数文档)
from fastapi import FastAPI, HTTPException, Path as ApiPath, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

# ChatHistoryStore：聊天记录 SQLite 存储（追加消息、按联系人查历史）
from .chat_history import ChatHistoryStore
# CommandConflictError：相同 commandId 配不同消息体的冲突异常；CommandLedger：命令幂等账本
from .command_ledger import CommandConflictError, CommandLedger
# QidianMappingStore：qidian 唤醒映射与唤醒日志（(siteId, supplierId) 主键）
from .qidian_mapping import QidianMappingStore
# 全部 Pydantic 请求/响应契约模型（API 的输入输出 schema）
from .contracts import IdentifyCurrentContactRequest, IdentifyCurrentContactResponse, API_VERSION, CaptureCheckResponse, ChatHistoryRequest, ChatHistoryResponse, CommandResponse, ListContactsRequest, ListContactsResponse, QidianMappingQuery, QidianMappingResponse, QidianWakeRequest, QidianWakeResponse, ReadMessagesRequest, ReadMessagesResponse, SendMessageRequest, ServiceHealthResponse
# QqAutomationPort：自动化端口抽象（测试时可注入替身）；LegacyQqAutomationFacade：真实实现（视觉 RPA）；QqAutomationError：QQ 操作失败统一异常
from .facade import LegacyQqAutomationFacade, QqAutomationError, QqAutomationPort, QqContactNotFoundError

logger = logging.getLogger("qq_service.api")
_request_id_var: ContextVar[str] = ContextVar("qq_request_id", default="-")

API_DESCRIPTION = """QQ 桌面自动化服务。\n\n所有操作只监听本机 127.0.0.1，不提供公网访问；发送接口使用 commandId 保证幂等。\n"""

OPENAPI_TAGS = [
    {"name": "系统", "description": "服务状态与截图可用性检查。"},
    {"name": "联系人", "description": "读取 QQ 用户列表。"},
    {"name": "消息", "description": "读取和发送 QQ 消息。"},
    {"name": "聊天记录", "description": "本地聊天记录查询。"},
    {"name": "qidian", "description": "接收 qidian 链接与公司信息，服务端自己唤醒 QQ 并登记接待人映射。"},
]


def create_app(
    automation: QqAutomationPort | None = None,
    *,
    ledger_path: Path,
    chat_history_path: Path | None = None,
    qidian_mapping_path: Path | None = None,
    layout_desktop=None,
) -> FastAPI:
    """构造 FastAPI 应用，注册全部 v1 接口并返回应用对象。

    参数：
    - automation：自动化实现（默认用真实视觉 RPA 门面；测试注入替身）；
    - ledger_path：命令账本数据库文件路径；
    - chat_history_path：聊天记录库路径（缺省自动放在账本同目录）；
    - qidian_mapping_path：qidian 唤醒映射库路径（缺省同样放在账本同目录）。
    """

    # 聊天记录库未显式指定时，默认与账本放同一目录，便于统一管理
    chat_history_path = chat_history_path or (ledger_path.parent / "qq-chat-history.sqlite3")
    # 初始化聊天记录存储（打开 SQLite、建表、启用 WAL）
    chat_history = ChatHistoryStore(chat_history_path)
    # qidian 映射库未显式指定时，同样与账本放同一目录，便于统一管理
    qidian_mapping_path = qidian_mapping_path or (ledger_path.parent / "qq-qidian-mappings.sqlite3")
    # 初始化 qidian 唤醒映射存储（打开 SQLite、建表）
    qidian_mappings = QidianMappingStore(qidian_mapping_path)
    # 自动化实现：外部传入了就用外部实现（测试/定制），否则用真实视觉 RPA 门面
    facade = automation or LegacyQqAutomationFacade()
    # 初始化命令账本（打开 SQLite、建表），用于发送命令的幂等控制
    ledger = CommandLedger(ledger_path)
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            close = getattr(facade, "close", None)
            if close is not None:
                await asyncio.to_thread(close)
                logger.info("automation.closed capture_released=true")
    # 创建 FastAPI 应用：开启默认 /docs(Swagger) 与 /openapi.json，版本号取契约常量
    app = FastAPI(
        title="QQ 桌面自动化服务",
        version=API_VERSION,
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )
    from utils.vision.layout_store import LayoutStore, LayoutError
    from .layouts import layout_router
    layout_store = LayoutStore(ledger_path.parent / "qq-layouts.sqlite3")
    if isinstance(facade, LegacyQqAutomationFacade):
        facade.layout_store = layout_store
    app.state.layout_store = layout_store
    app.include_router(layout_router(layout_store, layout_desktop))

    @app.exception_handler(LayoutError)
    async def layout_error_handler(request: Request, exc: LayoutError):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})
    logger.info(
        "application.create ledger_path=%s chat_history_path=%s qidian_mapping_path=%s automation=%s",
        ledger_path,
        chat_history_path,
        qidian_mapping_path,
        type(facade).__name__,
    )

    def record_attempt(
        operation: str, started: float, *, contact_name: str | None = None,
        status: str, failure_code: str | None = None, details: dict | None = None,
    ) -> None:
        try:
            chat_history.record_operation(
                request_id=_request_id_var.get(), operation=operation, contact_name=contact_name,
                status=status, failure_code=failure_code,
                duration_ms=(time.perf_counter() - started) * 1000, details=details,
            )
        except Exception:
            logger.exception("operation_attempt.write_failed operation=%s", operation)

    @app.middleware("http")
    async def log_request(request, call_next):
        """记录每个 HTTP 请求的 requestId、耗时、状态码和异常。"""
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        token = _request_id_var.set(request_id)
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
                "http.request.error request_id=%s method=%s path=%s duration_ms=%.2f total_duration_ms=%.2f",
                request_id,
                request.method,
                request.url.path,
                (time.perf_counter() - started) * 1000,
                (time.perf_counter() - started) * 1000,
            )
            _request_id_var.reset(token)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "http.request.end request_id=%s method=%s path=%s status=%d duration_ms=%.2f total_duration_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            elapsed_ms,
        )
        _request_id_var.reset(token)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        errors = [
            {"loc": list(error.get("loc", ())), "type": error.get("type"), "msg": error.get("msg")}
            for error in exc.errors()
        ]
        logger.warning(
            "http.request.validation_error method=%s path=%s errors=%s",
            request.method, request.url.path, errors,
        )
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

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

    @app.post("/v1/contacts/current:identify", response_model=IdentifyCurrentContactResponse, response_model_exclude_unset=True,
              summary="复制当前激活联系人的 QQ 号", tags=["联系人"])
    def identify_current_contact(request: IdentifyCurrentContactRequest | None = None):
        started = time.perf_counter()
        try:
            payload = IdentifyCurrentContactResponse.model_validate(facade.identify_current_contact())
        except QqAutomationError as exc:
            record_attempt("CONTACT_IDENTIFY", started, status="FAILED", failure_code=str(exc))
            logger.warning("contacts.identify.failed code=%s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        record_attempt("CONTACT_IDENTIFY", started, status="SKIPPED" if payload.skipped else "SUCCESS",
                       details={"reason": payload.reason} if payload.skipped else None)
        return payload

    # —— 读取联系人列表接口 ——
    @app.post(
        "/v1/contacts:query",
        response_model=ListContactsResponse,
        summary="查询 QQ 联系人列表",
        description="默认读取当前一屏的 QQ 用户列表（all 缺省为 false，最快）；指定联系人或当前会话"
                    "不可见时才滚动查找。all=true 时滚动扫掠整张会话名单，返回的 complete 标注是否"
                    "确认扫全。返回的 contacts key 可原样用于消息读取、发送和聊天记录查询。",
        tags=["联系人"],
        response_description="联系人列表、数量和是否完整",
    )
    def list_contacts(request: ListContactsRequest | None = None) -> ListContactsResponse:
        operation_started = time.perf_counter()
        try:
            # 同步端点：FastAPI 自动在（线程池）执行 QQ 视觉识别，事件循环不被阻塞
            payload = facade.list_contacts(
                request.contact_name if request else None,
                all_contacts=bool(request and request.all),
            )
            contacts = payload["contacts"]
            logger.info("contacts.query.success count=%d complete=%s",
                        len(contacts), payload["complete"])
        except QqAutomationError as exc:
            record_attempt("CONTACT_QUERY", operation_started, status="FAILED", failure_code=str(exc))
            logger.exception("contacts.query.error code=%s", exc)
            # QQ 操作失败（窗口不可用、识别失败等）统一返回 503，并把内部原因带给调用方
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        record_attempt("CONTACT_QUERY", operation_started, status="SUCCESS",
                       details={"count": len(contacts), "complete": payload["complete"]})
        # 组装响应：联系人数组 + 数量 + 是否完整名单
        return ListContactsResponse(
            contacts=contacts, count=len(contacts), complete=payload["complete"])

    # —— 读取指定联系人的可见消息接口 ——
    @app.post(
        "/v1/commands/read",
        response_model=ReadMessagesResponse,
        summary="读取指定联系人的可见消息",
        description="激活联系人并通过 QQ 框选复制读取当前可见消息窗口，成功后自动写入本地聊天记录。",
        tags=["消息"],
        response_description="当前可见消息列表",
    )
    def read_messages(request: ReadMessagesRequest) -> ReadMessagesResponse:
        operation_started = time.perf_counter()
        try:
            # 同步端点：FastAPI 自动在线程池执行 QQ 原生框选复制与文本解析
            payload = facade.read_messages(request.contact_name)
            logger.info(
                "messages.read.success contact=%s count=%d",
                request.contact_name,
                len(payload.get("messages", [])),
            )
        except QqContactNotFoundError as exc:
            record_attempt("READ", operation_started, contact_name=request.contact_name, status="FAILED", failure_code="QQ_CONTACT_NOT_FOUND")
            logger.info("messages.read.contact_not_found contact=%s", request.contact_name)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except QqAutomationError as exc:
            record_attempt("READ", operation_started, contact_name=request.contact_name, status="FAILED", failure_code=str(exc))
            logger.exception("messages.read.error contact=%s code=%s", request.contact_name, exc)
            # QQ 操作失败 → 503（不会把读取结果落库，因为根本没读到）
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # 读到的可见消息自动落库（供聊天记录查询），失败仅告警不阻断
        _record_payload(chat_history, request.contact_name, payload)
        record_attempt(
            "READ", operation_started, contact_name=request.contact_name, status="SUCCESS",
            details={"count": len(payload.get("messages", []))},
        )
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
        operation_started = time.perf_counter()
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
            record_attempt("SEND", operation_started, contact_name=request.contact_name, status="FAILED", failure_code=str(exc))
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
            record_attempt("SEND", operation_started, contact_name=request.contact_name, status="FAILED", failure_code=str(exc))
            ledger.resolve(request.command_id, "FAILED", result)
            # 返回 FAILED 状态给调用方
            return CommandResponse(commandId=request.command_id, status="FAILED", result=result)
        # 发送成功：账本落 SUCCEEDED 终态。聊天历史以后续 QQ 原生复制结果为准；
        # 服务不按正文猜测哪个复制记录对应这次发送。
        ledger.resolve(request.command_id, "SUCCEEDED", result)
        record_attempt("SEND", operation_started, contact_name=request.contact_name, status="SUCCESS")
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

    # —— 查询聊天记录接口（查询前自动通过 QQ 复制更新一次） ——
    @app.post(
        "/v1/chat/history",
        response_model=ChatHistoryResponse,
        summary="查询聊天记录",
        description="查询本地聊天记录。查询前会先框选复制当前消息；更新失败时返回旧数据并标记 update=failed。",
        tags=["聊天记录"],
        response_description="联系人聊天记录",
    )
    def query_chat_history(request: ChatHistoryRequest) -> ChatHistoryResponse:
        # 每次查询先做一次 QQ 原生复制（拉到最新消息），再返回历史，保证数据新鲜
        try:
            payload = facade.read_messages(request.contact_name)
        except QqAutomationError:
            # QQ 复制或文本解析失败：不阻断历史查询，只把更新标记为 failed 告知调用方
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
        # 返回 QQ 号、消息条数、消息列表与本次更新状态
        return ChatHistoryResponse(
            contactName=request.contact_name,
            count=len(messages),
            messages=messages,
            copiedText=payload.get("copiedText") if update == "ok" else None,
            update=update,
        )

    # —— qidian 唤醒接口（打开链接 → 等会话稳定 → 识别接待人 → 覆盖写入映射） ——
    @app.post(
        "/v1/qidian:wake",
        response_model=QidianWakeResponse,
        summary="唤醒 qidian 链接对应的 QQ 会话并登记接待人映射",
        description="用请求里的 targetQqNumber（调用方从华强搜索结果页读到的商家 QQ 号，必填）当目标号"
                    "（服务端不解析链接里的号）→ 用默认浏览器打开链接 → 等无障碍树会话名单"
                    "稳定并出现唯一激活行 → 复制当前接待人 QQ 号 → 按 (siteId, supplierId) 覆盖写入映射。"
                    "整段唤醒都在内部单消费者队列里串行执行，多个 qidian 同时到达不会交叉。"
                    "同一链接在 120 秒窗口内重复提交直接复用上次结果（reused=true），不会重复唤醒 QQ。",
        tags=["qidian"],
        responses={503: {"description": "唤醒失败 / 会话不稳定 / 读不到唯一激活行"}},
        response_description="本次（或命中的上次）唤醒结果与映射",
    )
    def wake_qidian(request: QidianWakeRequest) -> QidianWakeResponse:
        # 惰性导入：qidian 模块依赖 Windows 桌面能力，不在服务启动时加载
        from utils.qq.qidian import QidianError, link_hash, normalize_link

        operation_started = time.perf_counter()
        # 链接白名单校验放在路由层：非法链接属于参数违规（422），不进队列、不碰 QQ
        try:
            normalized = normalize_link(request.link)
        except QidianError as exc:
            record_attempt("QIDIAN_WAKE", operation_started, status="FAILED", failure_code=str(exc))
            logger.warning("qidian.wake.invalid_link link=%r code=%s", request.link, exc)
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        hash_ = link_hash(normalized)
        # 同链去重：120 秒窗口内同一链接直接复用上次结果，不重复唤醒
        recent = qidian_mappings.recent_wake(hash_, now_epoch=time.time())
        if recent is not None:
            qq_number = recent["qqNumber"]
            # 复用判定只认「同一条链接」；目标号以本次请求为准（同链不同号说明上一次的号有问题）
            target_qq = request.target_qq_number
            logger.info(
                "qidian.wake.reused site=%s supplier=%s hash=%s contact=%r target=%s",
                request.site_id, request.supplier_id, hash_[:12], recent["contactName"], target_qq,
            )
            record_attempt("QIDIAN_WAKE", operation_started, status="REUSED",
                           details={"linkHash": hash_, "contactName": recent["contactName"]})
            # 复用不写唤醒日志、不加 wakeCount，但把调用方这次传的目标号刷到映射行上，
            # 保证「响应里的 targetQqNumber」与「落库的映射」是同一个值。
            qidian_mappings.refresh_target(
                site_id=request.site_id,
                supplier_id=request.supplier_id,
                link_hash=hash_,
                supplier_name=request.supplier_name,
                contact_name=recent["contactName"],
                qq_number=qq_number,
                target_qq_number=target_qq,
                woke_at=recent["updatedAt"],
                woke_epoch=time.time(),
            )
            # 命中复用：接待人与 QQ 号来自上次唤醒日志，目标号取本次请求，reused=true 告知本次没有真的唤醒
            return QidianWakeResponse.model_validate({
                "linkHash": recent["linkHash"],
                "siteId": request.site_id,
                "supplierId": request.supplier_id,
                "supplierName": request.supplier_name,
                "contactName": recent["contactName"],
                "qqNumber": qq_number,
                "targetQqNumber": target_qq,
                "qqNumberVerified": qq_number is not None and qq_number == target_qq,
                "activeStable": True,
                "reused": True,
                "wokeAt": recent["updatedAt"],
            })

        # 真正唤醒：facade 走 Dispatcher 单消费者队列（视觉操作串行），失败投影成 503
        try:
            result = facade.wake_qidian(normalized, request.target_qq_number)
        except QqAutomationError as exc:
            record_attempt("QIDIAN_WAKE", operation_started, status="FAILED", failure_code=str(exc))
            logger.warning(
                "qidian.wake.failed site=%s supplier=%s code=%s duration_ms=%.2f",
                request.site_id, request.supplier_id, exc, (time.perf_counter() - operation_started) * 1000,
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        # 去重键以路由层算出的指纹为准（与 recent_wake 的查询键是同一个值）；
        # 实现层返回的 linkHash 是同一算法的复核，不一致说明链路有 bug，仅告警不改行为。
        if result.get("linkHash") not in (None, hash_):
            logger.warning("qidian.wake.hash_mismatch route=%s impl=%s", hash_, result.get("linkHash"))
        # 覆盖写入映射（旧值一并进唤醒日志）：同一 (siteId, supplierId) 直接覆盖，不返回冲突
        row = qidian_mappings.record_wake(
            site_id=request.site_id,
            supplier_id=request.supplier_id,
            link_hash=hash_,
            supplier_name=request.supplier_name,
            contact_name=result["contactName"],
            qq_number=result.get("qqNumber"),
            target_qq_number=result.get("targetQqNumber"),
            woke_at=result["wokeAt"],
            woke_epoch=time.time(),
        )
        record_attempt(
            "QIDIAN_WAKE", operation_started, contact_name=result["contactName"], status="SUCCESS",
            details={"linkHash": hash_, "qqNumber": result.get("qqNumber"),
                     "qqNumberVerified": result.get("qqNumberVerified"), "wakeCount": row["wakeCount"]},
        )
        logger.info(
            "qidian.wake.success site=%s supplier=%s contact=%r qq=%s target=%s verified=%s "
            "wake_count=%d duration_ms=%.2f",
            request.site_id, request.supplier_id, result["contactName"], result.get("qqNumber"),
            result.get("targetQqNumber"), result.get("qqNumberVerified"),
            row["wakeCount"], (time.perf_counter() - operation_started) * 1000,
        )
        # 组装响应：唤醒结果 + 本次请求携带的公司信息 + reused=false（linkHash 用路由层指纹覆盖）
        return QidianWakeResponse.model_validate({
            **result,
            "linkHash": hash_,
            "siteId": request.site_id,
            "supplierId": request.supplier_id,
            "supplierName": request.supplier_name,
            "reused": False,
        })

    # —— qidian 映射查询接口（纯本地 SQLite，不触碰 QQ） ——
    @app.post(
        "/v1/qidian/mappings:query",
        response_model=QidianMappingResponse,
        summary="查询 qidian 唤醒映射",
        description="按 (siteId, supplierId) 或 linkHash 读回最近一次 qidian 唤醒写入的映射；"
                    "没有记录时返回 found=false，其余字段为空。",
        tags=["qidian"],
        response_description="映射记录（found=false 表示该键还没有映射）",
    )
    def query_qidian_mapping(request: QidianMappingQuery) -> QidianMappingResponse:
        operation_started = time.perf_counter()
        # 本地 SQLite 短连接查询：不触碰 QQ，可与唤醒任务并发
        row = qidian_mappings.get(
            site_id=request.site_id, supplier_id=request.supplier_id, link_hash=request.link_hash,
        )
        if row is None:
            record_attempt("QIDIAN_MAPPING_QUERY", operation_started, status="NOT_FOUND")
            logger.info("qidian.mapping.miss site=%s supplier=%s hash=%s",
                        request.site_id, request.supplier_id, request.link_hash)
            # 没有映射不是错误：返回 found=false，其余字段保持为空
            return QidianMappingResponse(found=False)
        record_attempt(
            "QIDIAN_MAPPING_QUERY", operation_started, contact_name=row["contactName"], status="SUCCESS",
            details={"siteId": row["siteId"], "supplierId": row["supplierId"], "wakeCount": row["wakeCount"]},
        )
        return QidianMappingResponse.model_validate({**row, "found": True})

    # 返回构造好的应用对象（由调用方交给 uvicorn 托管）
    return app


def _record_payload(chat_history: ChatHistoryStore, contact_name: str, payload: dict) -> None:
    """把一次复制解析结果作为可见窗口差异写入聊天记录存储。"""
    try:
        inserted = chat_history.append_visible(contact_name, payload["messages"])
        logger.info("chat.history.append contact=%s inserted=%d", contact_name, inserted)
    except Exception:
        logger.exception("chat.history.append_failed contact=%s", contact_name)
