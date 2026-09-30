from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from service.app import create_app
from service.chat_history import ChatHistoryStore
from service.facade import QqAutomationError


def _message(sender: str, timestamp: str, text: str) -> dict:
    return {
        "sender": sender,
        "timestamp": timestamp,
        "text": text,
        "rawText": f"{sender}: {timestamp} {text}",
    }


@pytest.fixture
def store(tmp_path):
    return ChatHistoryStore(tmp_path / "chat.sqlite3")


def test_visible_window_keeps_repeated_messages(store):
    contact = "华强电子"
    first = [_message("TJJ", "09-30 12:00:00", "收到"), _message("TJJ", "09-30 12:00:01", "收到")]
    assert store.append_visible(contact, first) == 2
    assert [item["text"] for item in store.get_history(contact)] == ["收到", "收到"]

    assert store.append_visible(contact, first) == 0
    assert store.append_visible(contact, first + [_message("TJJ", "09-30 12:00:02", "收到")]) == 1


def test_visible_window_scroll_only_appends_new_tail(store):
    contact = "华强电子"
    messages = [_message("A", "09-30 12:00:01", "A"), _message("A", "09-30 12:00:02", "B")]
    store.append_visible(contact, messages)
    assert store.append_visible(contact, messages[1:] + [_message("B", "09-30 12:00:03", "C")]) == 1
    assert [item["text"] for item in store.get_history(contact)] == ["A", "B", "C"]


def test_history_keeps_sender_timestamp_and_raw_text(store):
    store.append_visible("华强电子", [_message("TJJ", "09-30 12:00:01", "请报价")])

    history = store.get_history("华强电子")
    assert history[0]["sender"] == "TJJ"
    assert history[0]["timestamp"] == "09-30 12:00:01"
    assert history[0]["rawText"] == "TJJ: 09-30 12:00:01 请报价"
    assert "direction" not in history[0]
    assert "commandId" not in history[0]


def test_existing_direction_database_is_migrated_without_returning_direction(tmp_path):
    chat_path = tmp_path / "chat.sqlite3"
    connection = sqlite3.connect(chat_path)
    connection.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_name TEXT NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('in', 'out', 'unknown')),
            sender TEXT,
            timestamp TEXT,
            text TEXT NOT NULL,
            raw_text TEXT,
            seq INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            command_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            CONSTRAINT uq_contact_seq UNIQUE (contact_name, seq)
        );
        INSERT INTO messages(contact_name, direction, sender, timestamp, text, raw_text, seq, fingerprint)
        VALUES ('华强电子', 'unknown', 'TJJ', '09-30 12:00:01', '旧消息', 'TJJ: 09-30 12:00:01 旧消息', 1, 'old');
        """
    )
    connection.commit()
    connection.close()

    history = ChatHistoryStore(chat_path).get_history("华强电子")
    assert history[0]["sender"] == "TJJ"
    assert history[0]["text"] == "旧消息"
    assert "direction" not in history[0]


class _RecordingAutomation:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.messages = [_message("TJJ", "09-30 12:00:01", "有货")]
        self.copied_text = "TJJ: 09-30 12:00:01 有货"

    def list_contacts(self) -> dict[str, dict]:
        return {"华强电子": {"name": "华强电子", "active": True, "new_msg": False}}

    def send_message(self, contact_name: str, text: str) -> dict:
        self.sent.append((contact_name, text))
        return {"ok": True, "sent": True, "textLength": len(text), "error": None}

    def read_messages(self, contact_name: str) -> dict:
        return {
            "ok": True,
            "messages": list(self.messages),
            "count": len(self.messages),
            "copiedText": self.copied_text,
            "error": None,
        }

    def check_capture_ready(self) -> dict:
        return {"ready": True, "windowTitle": "QQ"}


class _ReadFailAutomation(_RecordingAutomation):
    def read_messages(self, contact_name: str) -> dict:
        raise QqAutomationError("QQ_MESSAGES_UNAVAILABLE")


def test_send_does_not_fabricate_history_until_qq_copy_confirms_it(tmp_path):
    automation = _RecordingAutomation()
    chat_path = tmp_path / "chat.sqlite3"
    app = create_app(automation, ledger_path=tmp_path / "ledger.sqlite3", chat_history_path=chat_path)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qq-service.test") as caller:
            await caller.post(
                "/v1/commands/send",
                json={"commandId": "effect-key-0000100", "contactName": "华强电子", "text": "请报价"},
            )
            await caller.post("/v1/commands/read", json={"contactName": "华强电子"})

    asyncio.run(run())
    history = ChatHistoryStore(chat_path).get_history("华强电子")
    assert [item["text"] for item in history] == ["有货"]


def test_chat_history_query_auto_updates(tmp_path):
    app = create_app(
        _RecordingAutomation(), ledger_path=tmp_path / "ledger.sqlite3", chat_history_path=tmp_path / "chat.sqlite3"
    )

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qq-service.test") as caller:
            return await caller.post("/v1/chat/history", json={"contactName": "华强电子"})

    response = asyncio.run(run())
    assert response.status_code == 200
    body = response.json()
    assert body["update"] == "ok"
    assert body["copiedText"] == "TJJ: 09-30 12:00:01 有货"
    assert body["messages"][0]["sender"] == "TJJ"
    assert "direction" not in body["messages"][0]


def test_chat_history_query_update_failure_keeps_history(tmp_path):
    chat_path = tmp_path / "chat.sqlite3"
    store = ChatHistoryStore(chat_path)
    store.append_visible("华强电子", [_message("TJJ", "09-30 12:00:01", "历史消息")])
    app = create_app(_ReadFailAutomation(), ledger_path=tmp_path / "ledger.sqlite3", chat_history_path=chat_path)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qq-service.test") as caller:
            return await caller.post("/v1/chat/history", json={"contactName": "华强电子"})

    response = asyncio.run(run())
    assert response.status_code == 200
    assert response.json()["update"] == "failed"
    assert response.json()["messages"][0]["text"] == "历史消息"


def test_history_returns_latest_page(store):
    store.append_visible(
        "华强电子",
        [_message("TJJ", "09-30 12:00:01", "a"), _message("TJJ", "09-30 12:00:02", "b"), _message("TJJ", "09-30 12:00:03", "c")],
    )
    latest = store.get_history("华强电子", limit=2)
    assert [item["text"] for item in latest] == ["b", "c"]
    assert [item["seq"] for item in latest] == [2, 3]
