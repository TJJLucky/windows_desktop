"""QQ Desktop Service 的外部 HTTP 接口层。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status

from .chat_history import ChatHistoryStore
from .command_ledger import CommandConflictError, CommandLedger
from .contracts import API_VERSION, ChatHistoryRequest, ChatHistoryResponse, CommandResponse, ListContactsResponse, ReadMessagesRequest, ReadMessagesResponse, SendMessageRequest, ServiceHealthResponse
from .facade import LegacyQqAutomationFacade, QqAutomationError, QqAutomationPort


def create_app(
    automation: QqAutomationPort | None = None,
    *,
    token: str,
    ledger_path: Path,
    chat_history_path: Path | None = None,
) -> FastAPI:
    """创建受每次启动 token 保护的 v1 API。"""

    if not token:
        raise ValueError("QQ Service token must not be empty")
    chat_history_path = chat_history_path or (ledger_path.parent / "qq-chat-history.sqlite3")
    chat_history = ChatHistoryStore(chat_history_path)
    facade = automation or LegacyQqAutomationFacade()
    ledger = CommandLedger(ledger_path)
    app = FastAPI(title="PriceAgent QQ Desktop Service", version=API_VERSION, docs_url=None, redoc_url=None)

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="QQ_SERVICE_UNAUTHORIZED")

    @app.get("/v1/health", response_model=ServiceHealthResponse)
    async def health(_: None = Depends(require_token)) -> ServiceHealthResponse:
        return ServiceHealthResponse(apiVersion=API_VERSION, status="READY")

    @app.post("/v1/contacts:query", response_model=ListContactsResponse)
    async def list_contacts(_: None = Depends(require_token)) -> ListContactsResponse:
        try:
            contacts = await asyncio.to_thread(facade.list_contacts)
        except QqAutomationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return ListContactsResponse(contacts=contacts, count=len(contacts))

    @app.post("/v1/commands/read", response_model=ReadMessagesResponse)
    async def read_messages(request: ReadMessagesRequest, _: None = Depends(require_token)) -> ReadMessagesResponse:
        try:
            payload = await asyncio.to_thread(facade.read_messages, request.contact_name)
        except QqAutomationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        _record_payload(chat_history, request.contact_name, payload)
        return ReadMessagesResponse.model_validate(payload)

    @app.post("/v1/commands/send", response_model=CommandResponse)
    async def send_message(request: SendMessageRequest, _: None = Depends(require_token)) -> CommandResponse:
        try:
            execute, current_status, result = await asyncio.to_thread(
                ledger.begin, request.command_id, request.contact_name, request.text
            )
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not execute:
            return CommandResponse(commandId=request.command_id, status=current_status, result=result)
        try:
            result = await asyncio.to_thread(facade.send_message, request.contact_name, request.text)
        except QqAutomationError as exc:
            result = {"ok": False, "sent": False, "textLength": len(request.text), "error": str(exc)}
            await asyncio.to_thread(ledger.resolve, request.command_id, "FAILED", result)
            return CommandResponse(commandId=request.command_id, status="FAILED", result=result)
        await asyncio.to_thread(ledger.resolve, request.command_id, "SUCCEEDED", result)
        _try_record(chat_history, request.contact_name, "out", request.text, request.command_id)
        return CommandResponse(commandId=request.command_id, status="SUCCEEDED", result=result)

    @app.get("/v1/commands/{command_id}", response_model=CommandResponse)
    async def command_status(command_id: str, _: None = Depends(require_token)) -> CommandResponse:
        record = await asyncio.to_thread(ledger.get, command_id)
        if record is None:
            raise HTTPException(status_code=404, detail="QQ_COMMAND_NOT_FOUND")
        current_status, result = record
        return CommandResponse(commandId=command_id, status=current_status, result=result)

    @app.post("/v1/chat/history", response_model=ChatHistoryResponse)
    async def query_chat_history(request: ChatHistoryRequest, _: None = Depends(require_token)) -> ChatHistoryResponse:
        """每次查询聊天记录时，先自动视觉读取一次（更新存储），再返回最新历史。"""
        try:
            payload = await asyncio.to_thread(facade.read_messages, request.contact_name)
        except QqAutomationError:
            update = "failed"  # 自动更新失败不阻断历史查询
        else:
            _record_payload(chat_history, request.contact_name, payload)
            update = "ok"
        messages = chat_history.get_history(request.contact_name)
        return ChatHistoryResponse(
            contactName=request.contact_name, count=len(messages), messages=messages, update=update
        )

    return app


def _record_payload(chat_history: ChatHistoryStore, contact_name: str, payload: dict) -> None:
    """把一次读取结果（可见消息）写入聊天记录。"""
    for item in payload["messages"]:
        _try_record(chat_history, contact_name, "out" if item["isSelf"] else "in", item["text"])


def _try_record(
    chat_history: ChatHistoryStore,
    contact_name: str,
    direction: str,
    text: str,
    command_id: str | None = None,
) -> None:
    """聊天记录写入失败仅告警，不阻断主流程。"""
    try:
        chat_history.append(contact_name, direction, text, command_id)
    except Exception as exc:
        print(f"[WARN] 聊天记录写入失败: {exc}")
