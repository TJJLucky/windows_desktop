"""可选跨仓库契约测试：PYTHONPATH 加入主 Runtime 的 src 后启用，无真实 QQ 操作。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

runtime_callbacks = pytest.importorskip("agent_runtime.integrations.qq_service.callbacks")

from agent_runtime.http.app import create_app as runtime_app
from agent_runtime.integrations.qq_service import QQServiceEndpointResolver, QqDesktopServiceClient
import agent_runtime.integrations.qq_service as runtime_qq
from agent_runtime.models import RuntimeCredentials
from agent_runtime.skills.windows_desktop.main import ReadQQMessagesTool, SendQQMessageTool
from agent_runtime.skills.windows_desktop.schemas import ReadMessagesArgs, SendMessageArgs
from agent_runtime.tools.foundation import ToolExecutionContext

from service.app import create_app as service_app


class backend:
    def __init__(self):
        self.sent = []
        self.messages = [{"text": "历史消息", "sender": "供应商", "timestamp": "2026-10-08 09:00:00",
                          "rawText": "供应商 2026-10-08 09:00:00\n历史消息"}]

    def identity_snapshot(self):
        return {"sessionKey": "qq-process-and-account-evidence", "sessionEvidence": {"avatarHash": "account-avatar"},
                "contacts": [{"sourceKey": "merchant-avatar-and-name", "name": "供应商",
                              "active": True, "new_msg": False,
                              "evidence": {"avatarHash": "merchant-avatar", "name": "供应商"}}]}

    def bound_read(self, expected):
        assert expected["sourceKey"] == "merchant-avatar-and-name"
        return {"headerName": "供应商完整标题", "messages": list(self.messages)}

    def bound_send(self, expected, text):
        assert expected["headerName"] == "供应商完整标题"
        self.sent.append((expected["name"], text))
        return {"ok": True, "sent": True}


class cloud:
    def __init__(self):
        self.events = {}
        self.active = True

    async def publish_runtime_event(self, credentials, **event):
        previous = self.events.get(event["event_id"])
        assert previous is None or previous == event
        self.events[event["event_id"]] = event
        return {"accepted": True, "sequence": event["sequence"]}

    async def qq_subscription_state(self, credentials, task_ids, *, work_id=None):
        return {"tasks": [{"taskId": task_id, "workId": "work-1", "active": self.active} for task_id in task_ids],
                "workflowActive": self.active}


class worker:
    async def shutdown(self):
        pass


@pytest.mark.asyncio
async def test_runtime_tools_service_queue_and_authenticated_durable_callback_end_to_end(tmp_path, monkeypatch):
    automation = backend()
    service = service_app(automation, ledger_path=tmp_path / "service.sqlite3")
    endpoint = tmp_path / "endpoint.json"
    endpoint.write_text(json.dumps({"schemaVersion": 1, "service": "price-agent-qq-service", "apiVersion": "v1",
                                    "processId": 1, "endpoint": "http://127.0.0.1:51111"}), encoding="utf-8")
    client = QqDesktopServiceClient(resolver=QQServiceEndpointResolver(endpoint, pid_alive=lambda _: True),
                                    transport=httpx.ASGITransport(service))
    credentials = RuntimeCredentials(cloudBaseUrl="https://cloud.example", accessToken="private-jwt", userId="user-1")
    inbox = cloud()

    async def get_credentials():
        return credentials

    relay = runtime_callbacks.QqCallbackRelay(
        state_file=tmp_path / "runtime-callbacks.json", callback_url="http://127.0.0.1:50123/api/local-runtime/qq/events",
        credentials_provider=get_credentials, cloud=inbox, client_provider=lambda: client,
    )
    local = runtime_app(SimpleNamespace(), worker(), qq_callback_handler=relay)
    service.state.callbacks.transport = httpx.ASGITransport(local)
    monkeypatch.setattr(runtime_qq, "_port", client)
    runtime_callbacks.configure_qq_callbacks(relay)
    context = ToolExecutionContext(
        tool_call_id="call-1", effect_key="frozen-command-id-000001", cancel_event=asyncio.Event(),
        authorize_effect_callback=lambda _: asyncio.sleep(0, result=True), credentials=credentials,
        job=SimpleNamespace(task_context={"role": "MAIN", "workId": "work-1"}, job_id="job-1", attempt=1),
    )
    snapshot = await client.list_conversations()
    contact = next(iter(snapshot["contacts"].values()))
    assert contact["identityKind"] == "SERVICE_UI_EVIDENCE"
    args = {"task_id": "merchant-1", "session_id": contact["sessionId"],
            "conversation_id": contact["conversationId"], "contact_name": "供应商"}
    try:
        async with service.router.lifespan_context(service):
            read = await ReadQQMessagesTool().execute(ReadMessagesArgs(**args), context)
            assert read.payload["header_name"] == "供应商完整标题"
            assert read.payload["after_sequence"] == 1
            sent = await SendQQMessageTool().execute(SendMessageArgs(**args, text="请报含税单价", after_sequence=read.payload["after_sequence"]), context)
            assert sent.deferred.event_type == "command.completed"
            for _ in range(100):
                if inbox.events:
                    break
                await asyncio.sleep(.01)
            assert len(inbox.events) == 1
            complete = next(iter(inbox.events.values()))
            assert complete["payload"]["sent"] is True
            assert complete["wait_key"] == "qq-command:frozen-command-id-000001"
            automation.messages.append({"text": "含税单价 10 元", "sender": "供应商", "timestamp": "2026-10-08 10:00:00",
                                        "rawText": "供应商 2026-10-08 10:00:00\n含税单价 10 元"})
            for _ in range(300):
                if len(inbox.events) == 2:
                    break
                await asyncio.sleep(.01)
            reply = [value for value in inbox.events.values() if value["event_type"] == "message.received"][0]
            assert reply["payload"]["message"]["isSelf"] is None
            assert reply["payload"]["taskId"] == "merchant-1"
            assert reply["payload"]["message"]["sequence"] == 2
            assert reply["payload"]["message"]["rawText"].endswith("含税单价 10 元")
            assert len(automation.sent) == 1
            inbox.active = False
            stopped = await relay.cleanup_work("work-1")
            assert stopped["status"] == "CLEANED"
            assert not service.state.callback_store.active_message_subscriptions()
    finally:
        runtime_callbacks.configure_qq_callbacks(None)
