"""聊天记录持久化：把读到的（in）与发出的（out）消息写入本地 SQLite。

与 command_ledger 同约定：标准库 sqlite3、短连接、threading.Lock 串行化。
默认文件位于 <state-dir>/qq-chat-history.sqlite3（独立文件，避免账本锁竞争）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

_DEDUP_WINDOW_SECONDS = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_name TEXT    NOT NULL,
    direction    TEXT    NOT NULL CHECK (direction IN ('in', 'out')),
    text         TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    fingerprint  TEXT    NOT NULL,
    command_id   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CONSTRAINT uq_contact_seq UNIQUE (contact_name, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_contact_time
    ON messages (contact_name, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_contact_text_time
    ON messages (contact_name, direction, text, created_at);
"""


def _fingerprint(contact_name: str, direction: str, text: str) -> str:
    return hashlib.sha256(f"{contact_name}|{direction}|{text}".encode("utf-8")).hexdigest()


class ChatHistoryStore:
    """QQ 聊天记录存储（单文件 SQLite，短连接 + 锁串行化）。"""

    def __init__(self, path: Path, dedup_window: int = _DEDUP_WINDOW_SECONDS) -> None:
        self._path = path
        self._dedup_window = dedup_window
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    def append(self, contact_name: str, direction: str, text: str, command_id: str | None = None) -> bool:
        """写一条消息：近窗查重 → seq 递增 → 插入。

        :return: True=已写入；False=去重窗口内同联系人同方向同文本（重复读取）已跳过
        """
        if direction not in ("in", "out"):
            raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
        text = str(text or "")
        with self._lock, closing(self._connect()) as connection, connection:
            duplicate = connection.execute(
                "SELECT id FROM messages"
                " WHERE contact_name = ? AND direction = ? AND text = ?"
                " AND created_at > datetime('now', 'localtime', ?)"
                " ORDER BY id DESC LIMIT 1",
                (contact_name, direction, text, f"-{self._dedup_window} seconds"),
            ).fetchone()
            if duplicate is not None:
                return False
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE contact_name = ?",
                (contact_name,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO messages(contact_name, direction, text, seq, fingerprint, command_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (contact_name, direction, text, seq,
                 _fingerprint(contact_name, direction, text), command_id),
            )
        return True

    def list_conversations(self, limit: int = 50) -> list[dict]:
        """会话列表：每个联系人最后一条时间、总条数、最后一条文本（按最后时间倒序）。"""
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT m1.contact_name,"
                "       MAX(m1.created_at) AS last_time,"
                "       COUNT(*) AS total,"
                "       (SELECT m2.text FROM messages m2"
                "         WHERE m2.contact_name = m1.contact_name"
                "         ORDER BY m2.seq DESC LIMIT 1) AS last_text"
                " FROM messages m1"
                " GROUP BY m1.contact_name"
                " ORDER BY last_time DESC"
                " LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"contactName": r[0], "lastTime": r[1], "total": r[2], "lastText": r[3]}
            for r in rows
        ]

    def get_history(self, contact_name: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """单联系人历史消息（按会话序号升序分页）。"""
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, direction, text, seq, command_id, created_at"
                " FROM messages WHERE contact_name = ?"
                " ORDER BY seq ASC LIMIT ? OFFSET ?",
                (contact_name, limit, offset),
            ).fetchall()
        return [
            {"id": r[0], "direction": r[1], "text": r[2], "seq": r[3],
             "commandId": r[4], "createdAt": r[5]}
            for r in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)
