"""聊天记录存储与 facade 接入测试；不操作真实 QQ。"""

from __future__ import annotations

import httpx
import pytest

from service.app import create_app
from service.chat_history import ChatHistoryStore
from service.facade import QqAutomationError


@pytest.fixture
def store(tmp_path):
    return ChatHistoryStore(tmp_path / "chat.sqlite3")


def test_append_and_dedup(store):
    assert store.append("华强电子", "in", "有货") is True
    assert store.append("华强电子", "in", "有货") is False   # 60s 内重复读取 → 跳过
    assert store.append("华强电子", "out", "收到", "cmd-1") is True
    assert store.append("张三", "in", "有货") is True        # 不同联系人同文本 → 写入


def test_seq_per_contact(store):
    store.append("华强电子", "in", "a")
    store.append("华强电子", "in", "b")
    store.append("张三", "in", "a")
    assert [m["seq"] for m in store.get_history("华强电子")] == [1, 2]
    assert store.get_history("张三")[0]["seq"] == 1


def test_conversations(store):
    store.append("华强电子", "in", "有货")
    store.append("华强电子", "out", "收到")
    conv = store.list_conversations()
    assert len(conv) == 1
    assert conv[0]["total"] == 2 and conv[0]["lastText"] == "收到"


def test_direction_validation(store):
    with pytest.raises(ValueError):
        store.append("华强电子", "side", "x")


class _RecordingAutomation:
    """与 FakeAutomation 同契约：记录发送并回读消息。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def list_contacts(self) -> dict[str, dict]:
        return {"华强电子": {"name": "华强电子", "active": True, "new_msg": False}}

    def send_message(self, contact_name: str, text: str) -> dict:
        self.sent.append((contact_name, text))
        return {"ok": True, "sent": True, "textLength": len(text), "error": None}

    def read_messages(self, contact_name: str) -> dict:
        return {
            "ok": True,
            "messages": [{"text": "有货", "isSelf": False, "x": 1, "y": 2, "w": 3, "h": 4}],
            "count": 1,
            "error": None,
        }


class _ReadFailAutomation(_RecordingAutomation):
    """视觉读取失败的替身：自动更新降级场景。"""

    def read_messages(self, contact_name: str) -> dict:
        raise QqAutomationError("QQ_MESSAGES_UNAVAILABLE")


def test_create_app_writes_chat_history(tmp_path):
    """API 层集成：read/send 成功后 store 落库（不触真实 QQ）。"""
    automation = _RecordingAutomation()
    ledger_path = tmp_path / "ledger.sqlite3"
    chat_path = tmp_path / "chat.sqlite3"
    app = create_app(automation, token="test-token", ledger_path=ledger_path, chat_history_path=chat_path)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://qq-service.test",
            headers={"Authorization": "Bearer test-token"},
        ) as caller:
            await caller.post(
                "/v1/commands/send",
                json={"commandId": "effect-key-0000100", "contactName": "华强电子", "text": "请报价"},
            )
            await caller.post("/v1/commands/read", json={"contactName": "华强电子"})

    import asyncio

    asyncio.run(run())
    store = ChatHistoryStore(chat_path)
    history = store.get_history("华强电子")
    assert len(history) == 2
    assert history[0]["direction"] == "out" and history[0]["commandId"] == "effect-key-0000100"
    assert history[1]["direction"] == "in" and history[1]["text"] == "有货"


def test_chat_history_query_auto_updates(tmp_path):
    """查询聊天记录：先自动更新一次（读取落库），再返回历史。"""
    chat_path = tmp_path / "chat.sqlite3"
    app = create_app(
        _RecordingAutomation(), token="test-token",
        ledger_path=tmp_path / "ledger.sqlite3", chat_history_path=chat_path,
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://qq-service.test",
            headers={"Authorization": "Bearer test-token"},
        ) as caller:
            return await caller.post("/v1/chat/history", json={"contactName": "华强电子"})

    import asyncio

    response = asyncio.run(run())
    assert response.status_code == 200
    body = response.json()
    assert body["update"] == "ok"
    assert body["count"] == 1
    assert body["messages"][0]["direction"] == "in"
    assert body["messages"][0]["text"] == "有货"
    # 已落库：再次查询不重复入库（去重生效）
    response = asyncio.run(run())
    assert response.json()["count"] == 1


def test_chat_history_query_update_failure_keeps_history(tmp_path):
    """自动更新失败（QQ 读取异常）不阻断历史查询，update=failed。"""
    chat_path = tmp_path / "chat.sqlite3"
    store = ChatHistoryStore(chat_path)
    store.append("华强电子", "out", "历史消息", "effect-key-0000200")
    app = create_app(
        _ReadFailAutomation(), token="test-token",
        ledger_path=tmp_path / "ledger.sqlite3", chat_history_path=chat_path,
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://qq-service.test",
            headers={"Authorization": "Bearer test-token"},
        ) as caller:
            return await caller.post("/v1/chat/history", json={"contactName": "华强电子"})

    import asyncio

    response = asyncio.run(run())
    assert response.status_code == 200
    body = response.json()
    assert body["update"] == "failed"
    assert body["count"] == 1
    assert body["messages"][0]["text"] == "历史消息"
