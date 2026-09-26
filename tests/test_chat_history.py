"""聊天记录存储与 service 接入测试；不操作真实 QQ。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from service.app import create_app
from service.chat_history import ChatHistoryStore
from service.facade import QqAutomationError


def _message(text: str, is_self: bool = False) -> dict:
    return {"text": text, "isSelf": is_self, "x": 1, "y": 2, "w": 3, "h": 4}


@pytest.fixture
def store(tmp_path):
    return ChatHistoryStore(tmp_path / "chat.sqlite3")


def test_visible_window_keeps_repeated_messages(store):
    contact = "华强电子"
    assert store.append_visible(contact, [_message("收到"), _message("收到")]) == 2
    assert [item["text"] for item in store.get_history(contact)] == ["收到", "收到"]

    # 同一个窗口重复读取不新增。
    assert store.append_visible(contact, [_message("收到"), _message("收到")]) == 0

    # 窗口尾部新增一条相同文本，仍然应新增。
    assert store.append_visible(contact, [_message("收到"), _message("收到"), _message("收到")]) == 1
    assert [item["text"] for item in store.get_history(contact)] == ["收到", "收到", "收到"]


def test_visible_window_scroll_only_appends_new_tail(store):
    contact = "华强电子"
    store.append_visible(contact, [_message("A"), _message("B"), _message("C")])
    assert store.append_visible(contact, [_message("B"), _message("C"), _message("D")]) == 1
    assert [item["text"] for item in store.get_history(contact)] == ["A", "B", "C", "D"]


def test_seq_per_contact(store):
    store.append_visible("华强电子", [_message("a"), _message("b")])
    store.append_visible("张三", [_message("a")])
    assert [item["seq"] for item in store.get_history("华强电子")] == [1, 2]
    assert store.get_history("张三")[0]["seq"] == 1


def test_conversations(store):
    store.append_visible("华强电子", [_message("有货")])
    store.append_visible("华强电子", [_message("有货"), _message("收到", True)])
    conversations = store.list_conversations()
    assert len(conversations) == 1
    assert conversations[0]["total"] == 2
    assert conversations[0]["lastText"] == "收到"


def test_pending_outbound_is_linked_when_visible(store):
    contact = "华强电子"
    store.mark_outbound(contact, "请报价", "effect-key-0000100")
    assert store.append_visible(contact, [_message("请报价", True)]) == 1
    history = store.get_history(contact)
    assert history[0]["direction"] == "out"
    assert history[0]["commandId"] == "effect-key-0000100"

class _RecordingAutomation:
    """与 FakeAutomation 同契约：记录发送，并让读取结果反映发送后的可见窗口。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.messages: list[dict] = [_message("有货")]

    def list_contacts(self) -> dict[str, dict]:
        return {"华强电子": {"name": "华强电子", "active": True, "new_msg": False}}

    def send_message(self, contact_name: str, text: str) -> dict:
        self.sent.append((contact_name, text))
        self.messages.append(_message(text, True))
        return {"ok": True, "sent": True, "textLength": len(text), "error": None}

    def read_messages(self, contact_name: str) -> dict:
        messages = list(self.messages)
        return {"ok": True, "messages": messages, "count": len(messages), "error": None}

    def check_capture_ready(self) -> dict:
        return {"ready": True, "windowTitle": "QQ"}


class _ReadFailAutomation(_RecordingAutomation):
    def read_messages(self, contact_name: str) -> dict:
        raise QqAutomationError("QQ_MESSAGES_UNAVAILABLE")


def test_create_app_writes_chat_history(tmp_path):
    automation = _RecordingAutomation()
    chat_path = tmp_path / "chat.sqlite3"
    app = create_app(automation, ledger_path=tmp_path / "ledger.sqlite3", chat_history_path=chat_path)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://qq-service.test",
        ) as caller:
            await caller.post(
                "/v1/commands/send",
                json={"commandId": "effect-key-0000100", "contactName": "华强电子", "text": "请报价"},
            )
            await caller.post("/v1/commands/read", json={"contactName": "华强电子"})

    asyncio.run(run())
    history = ChatHistoryStore(chat_path).get_history("华强电子")
    assert [item["direction"] for item in history] == ["in", "out"]
    assert history[1]["commandId"] == "effect-key-0000100"


def test_chat_history_query_auto_updates(tmp_path):
    chat_path = tmp_path / "chat.sqlite3"
    app = create_app(
        _RecordingAutomation(),
        ledger_path=tmp_path / "ledger.sqlite3",
        chat_history_path=chat_path,
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://qq-service.test",
        ) as caller:
            return await caller.post("/v1/chat/history", json={"contactName": "华强电子"})

    first = asyncio.run(run())
    assert first.status_code == 200
    assert first.json()["update"] == "ok"
    assert first.json()["count"] == 1
    assert first.json()["messages"][0]["direction"] == "in"

    second = asyncio.run(run())
    assert second.json()["count"] == 1


def test_chat_history_query_update_failure_keeps_history(tmp_path):
    chat_path = tmp_path / "chat.sqlite3"
    store = ChatHistoryStore(chat_path)
    store.append_visible("华强电子", [_message("历史消息", True)])
    app = create_app(
        _ReadFailAutomation(),
        ledger_path=tmp_path / "ledger.sqlite3",
        chat_history_path=chat_path,
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://qq-service.test",
        ) as caller:
            return await caller.post("/v1/chat/history", json={"contactName": "华强电子"})

    response = asyncio.run(run())
    assert response.status_code == 200
    body = response.json()
    assert body["update"] == "failed"
    assert body["count"] == 1
    assert body["messages"][0]["text"] == "历史消息"

def test_existing_history_seeds_first_visible_snapshot(tmp_path):
    """旧版本数据库没有 contact_snapshot 时，首次读取不能重复插入已有消息。"""
    import sqlite3

    chat_path = tmp_path / "chat.sqlite3"
    store = ChatHistoryStore(chat_path)
    connection = sqlite3.connect(chat_path)
    connection.execute(
        "INSERT INTO messages(contact_name, direction, text, seq, fingerprint) VALUES (?, ?, ?, ?, ?)",
        ("华强电子", "in", "旧消息", 1, "old-fingerprint"),
    )
    connection.commit()
    connection.close()

    assert store.append_visible("华强电子", [_message("旧消息")]) == 0
    assert len(store.get_history("华强电子")) == 1


def test_history_returns_latest_page(store):
    store.append_visible("华强电子", [_message("a"), _message("b"), _message("c")])
    latest = store.get_history("华强电子", limit=2)
    assert [item["text"] for item in latest] == ["b", "c"]
    assert [item["seq"] for item in latest] == [2, 3]