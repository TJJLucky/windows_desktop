"""QQ Desktop Service 的外部 HTTP 接口层。

本模块定义服务暴露给调用方（UniApp 前端 / 调试工具）的全部 HTTP 接口：
- 本机 loopback 服务不做 token 鉴权：所有接口均可直接访问（安全由"仅监听 127.0.0.1"保证）；
- 阻塞型 QQ 操作（视觉识别 + 模拟点击）通过 asyncio.to_thread 放进线程池执行，避免阻塞事件循环；
- 发送命令走"命令账本"（command_ledger）保证幂等：相同 commandId 只执行一次；
- 聊天记录的读取与发送都会自动落库（chat_history），供后续查询。
"""

# 延迟求值类型注解：允许前向引用，加快 import 速度
from __future__ import annotations

# asyncio：提供 to_thread —— 把阻塞的 QQ 桌面操作丢到线程池，不卡住 HTTP 事件循环
import asyncio
# Path：数据库文件路径的类型标注
from pathlib import Path

# FastAPI 框架组件：FastAPI(应用对象) / HTTPException(标准错误响应)
from fastapi import FastAPI, HTTPException

# ChatHistoryStore：聊天记录 SQLite 存储（追加消息、按联系人查历史）
from .chat_history import ChatHistoryStore
# CommandConflictError：相同 commandId 配不同消息体的冲突异常；CommandLedger：命令幂等账本
from .command_ledger import CommandConflictError, CommandLedger
# 全部 Pydantic 请求/响应契约模型（API 的输入输出 schema）
from .contracts import API_VERSION, CaptureCheckResponse, ChatHistoryRequest, ChatHistoryResponse, CommandResponse, ListContactsResponse, ReadMessagesRequest, ReadMessagesResponse, SendMessageRequest, ServiceHealthResponse
# QqAutomationPort：自动化端口抽象（测试时可注入替身）；LegacyQqAutomationFacade：真实实现（视觉 RPA）；QqAutomationError：QQ 操作失败统一异常
from .facade import LegacyQqAutomationFacade, QqAutomationError, QqAutomationPort


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
    app = FastAPI(title="PriceAgent QQ Desktop Service", version=API_VERSION)

    # —— 健康检查接口 ——
    @app.get("/v1/health", response_model=ServiceHealthResponse)
    async def health() -> ServiceHealthResponse:
        # 返回固定状态 READY + API 版本；调用方用它探测服务是否存活
        return ServiceHealthResponse(apiVersion=API_VERSION, status="READY")

    # —— 截图可用性检查接口（探测） ——
    @app.post("/v1/capture:check", response_model=CaptureCheckResponse)
    async def check_capture() -> CaptureCheckResponse:
        # 窗口就绪检测同样是阻塞操作（进程/窗口枚举 + 可能唤起 + WGC 试截），丢线程池执行
        result = await asyncio.to_thread(facade.check_capture_ready)
        # 组装响应：ready 透传；windowTitle 就绪时才有；error 不可用时才有
        # 注意：窗口不可用返回 200 + ready=false（探测语义），不投影为 503
        return CaptureCheckResponse(
            ready=result["ready"],
            method="ensure_qq_window_with_retry",
            windowTitle=result.get("windowTitle"),
            error=result.get("error"),
        )

    # —— 读取联系人列表接口 ——
    @app.post("/v1/contacts:query", response_model=ListContactsResponse)
    async def list_contacts() -> ListContactsResponse:
        try:
            # QQ 视觉识别是阻塞操作：丢进线程池执行，防止整个事件循环被卡住
            contacts = await asyncio.to_thread(facade.list_contacts)
        except QqAutomationError as exc:
            # QQ 操作失败（窗口不可用、识别失败等）统一返回 503，并把内部原因带给调用方
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # 组装响应：联系人数组 + 数量
        return ListContactsResponse(contacts=contacts, count=len(contacts))

    # —— 读取指定联系人的可见消息接口 ——
    @app.post("/v1/commands/read", response_model=ReadMessagesResponse)
    async def read_messages(request: ReadMessagesRequest) -> ReadMessagesResponse:
        try:
            # 同样丢进线程池执行：视觉读取消息区 + OCR 识别文本
            payload = await asyncio.to_thread(facade.read_messages, request.contact_name)
        except QqAutomationError as exc:
            # QQ 操作失败 → 503（不会把读取结果落库，因为根本没读到）
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # 读到的可见消息自动落库（供聊天记录查询），失败仅告警不阻断
        _record_payload(chat_history, request.contact_name, payload)
        # 校验并返回读取结果（payload 是 dict，用模型校验后返回，保证 schema 一致性）
        return ReadMessagesResponse.model_validate(payload)

    # —— 发送消息接口（幂等） ——
    @app.post("/v1/commands/send", response_model=CommandResponse)
    async def send_message(request: SendMessageRequest) -> CommandResponse:
        try:
            # 先向账本申请执行：begin 返回 (是否真正执行, 当前状态, 已有结果)。
            # 相同 commandId 相同消息体 → 幂等返回已有结果不重发；相同 ID 不同消息体 → 抛 409
            execute, current_status, result = await asyncio.to_thread(
                ledger.begin, request.command_id, request.contact_name, request.text
            )
        except CommandConflictError as exc:
            # 冲突（同一 commandId 配不同消息体）→ 409，明确告知调用方
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not execute:
            # 账本判定无需再次执行（已在跑或已完成）：直接把当前状态与结果原样返回
            return CommandResponse(commandId=request.command_id, status=current_status, result=result)
        try:
            # 真正执行发送：线程池里跑视觉定位输入框 + 粘贴 + 点击发送
            result = await asyncio.to_thread(facade.send_message, request.contact_name, request.text)
        except QqAutomationError as exc:
            # 发送失败：构造失败结果并让账本落 FAILED 终态（不会留下悬空的 RUNNING）
            result = {"ok": False, "sent": False, "textLength": len(request.text), "error": str(exc)}
            await asyncio.to_thread(ledger.resolve, request.command_id, "FAILED", result)
            # 返回 FAILED 状态给调用方
            return CommandResponse(commandId=request.command_id, status="FAILED", result=result)
        # 发送成功：账本落 SUCCEEDED 终态
        await asyncio.to_thread(ledger.resolve, request.command_id, "SUCCEEDED", result)
        # 发出的消息自动落库为 out（我方发出），关联 commandId 便于追溯；失败仅告警
        _try_record(chat_history, request.contact_name, "out", request.text, request.command_id)
        # 返回 SUCCEEDED 状态 + 实际执行结果
        return CommandResponse(commandId=request.command_id, status="SUCCEEDED", result=result)

    # —— 查询发送命令状态接口 ——
    @app.get("/v1/commands/{command_id}", response_model=CommandResponse)
    async def command_status(command_id: str) -> CommandResponse:
        # 从账本按 commandId 查状态与结果
        record = await asyncio.to_thread(ledger.get, command_id)
        if record is None:
            # 账本中不存在该 commandId → 404，防止调用方把未知 ID 当成功
            raise HTTPException(status_code=404, detail="QQ_COMMAND_NOT_FOUND")
        # 解包 (当前状态, 结果) 并返回
        current_status, result = record
        return CommandResponse(commandId=command_id, status=current_status, result=result)

    # —— 查询聊天记录接口（查询前自动视觉更新一次） ——
    @app.post("/v1/chat/history", response_model=ChatHistoryResponse)
    async def query_chat_history(request: ChatHistoryRequest) -> ChatHistoryResponse:
        # 需求约定：每次查询先做一次视觉读取（拉到最新消息），再返回历史，保证数据新鲜
        try:
            payload = await asyncio.to_thread(facade.read_messages, request.contact_name)
        except QqAutomationError:
            # 视觉读取失败（如窗口不可用）：不阻断历史查询，只把更新标记为 failed 告知调用方
            update = "failed"  # 自动更新失败不阻断历史查询
        else:
            # 读取成功：把新消息落库，更新标记为 ok
            _record_payload(chat_history, request.contact_name, payload)
            update = "ok"
        # 从存储中取出该联系人的历史消息（按 seq 升序）
        messages = chat_history.get_history(request.contact_name)
        # 返回联系人名、消息条数、消息列表与本次更新状态
        return ChatHistoryResponse(
            contactName=request.contact_name, count=len(messages), messages=messages, update=update
        )

    # 返回构造好的应用对象（由调用方交给 uvicorn 托管）
    return app


def _record_payload(chat_history: ChatHistoryStore, contact_name: str, payload: dict) -> None:
    """把一次视觉读取结果（可见消息列表）逐条写入聊天记录存储。"""
    # 遍历读取到的每一条消息
    for item in payload["messages"]:
        # 方向判定：isSelf=True 表示我方发出(out)，否则是对方发来(in)；逐条落库
        _try_record(chat_history, contact_name, "out" if item["isSelf"] else "in", item["text"])


def _try_record(
    chat_history: ChatHistoryStore,
    contact_name: str,
    direction: str,
    text: str,
    command_id: str | None = None,
) -> None:
    """写入一条聊天记录；写入失败仅打印告警，绝不阻断主流程（存储是增强能力，不是核心链路）。"""
    try:
        # 调用存储追加一条消息（内部含 60s 窗口去重、seq 递增、指纹等逻辑）
        chat_history.append(contact_name, direction, text, command_id)
    except Exception as exc:
        # 任何存储异常（磁盘满、锁冲突等）都降级为告警，主流程（读取/发送）继续
        print(f"[WARN] 聊天记录写入失败: {exc}")
