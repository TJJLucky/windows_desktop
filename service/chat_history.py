"""QQ 复制文本的本地聊天记录存储。

每次读取只处理 QQ 当前可见窗口的原生复制结果。消息以 sender、timestamp、
text 和 rawText 为事实字段；服务不判断消息方向，外部智能体可按 sender 自行判断。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any


_MESSAGES_TABLE_SQL = """
CREATE TABLE messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_name TEXT    NOT NULL,
    sender       TEXT,
    timestamp    TEXT,
    text         TEXT    NOT NULL,
    raw_text     TEXT,
    seq          INTEGER NOT NULL,
    fingerprint  TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CONSTRAINT uq_contact_seq UNIQUE (contact_name, seq)
)
"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_name TEXT    NOT NULL,
    sender       TEXT,
    timestamp    TEXT,
    text         TEXT    NOT NULL,
    raw_text     TEXT,
    seq          INTEGER NOT NULL,
    fingerprint  TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    CONSTRAINT uq_contact_seq UNIQUE (contact_name, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_contact_time
    ON messages (contact_name, created_at);

CREATE TABLE IF NOT EXISTS contact_snapshot (
    contact_name TEXT PRIMARY KEY,
    messages_json TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def _fingerprint(contact_name: str, sender: str, timestamp: str, text: str, raw_text: str) -> str:
    """生成稳定指纹，保留给排查和后续迁移使用。"""
    source = f"{contact_name}|{sender}|{timestamp}|{text}|{raw_text}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _normalise_visible(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """规范化复制解析结果，保留 QQ 原始发送人、时间、正文和单条原文。"""
    result: list[dict[str, str]] = []
    for item in messages:
        text = str(item.get("text") or "")
        if not text:
            continue
        result.append(
            {
                "sender": str(item.get("sender") or ""),
                "timestamp": str(item.get("timestamp") or ""),
                "text": text,
                "rawText": str(item.get("rawText") or item.get("raw_text") or ""),
            }
        )
    return result


def _message_key(message: dict[str, str]) -> tuple[str, str, str]:
    """窗口对齐身份：QQ 复制的时间、发送人和正文。"""
    return (
        message.get("timestamp", "").strip(),
        message.get("sender", "").strip(),
        message.get("text", "").strip(),
    )


def _suffix_prefix_overlap(previous: list[dict[str, str]], current: list[dict[str, str]]) -> int:
    """返回 previous 尾部与 current 头部的最长连续重叠长度。"""
    limit = min(len(previous), len(current))
    for size in range(limit, 0, -1):
        if previous[len(previous) - size:] == current[:size]:
            return size
    return 0


def _longest_common_subsequence_overlap(previous: list[dict[str, str]], current: list[dict[str, str]]) -> int:
    """返回 current 中最后一个已见消息的下一个下标。"""
    if not previous or not current:
        return 0
    overlap = _suffix_prefix_overlap(previous, current)
    if overlap:
        return overlap

    dp = [[0] * (len(current) + 1) for _ in range(len(previous) + 1)]
    for i in range(1, len(previous) + 1):
        previous_key = _message_key(previous[i - 1])
        for j in range(1, len(current) + 1):
            if previous_key == _message_key(current[j - 1]):
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    i, j = len(previous), len(current)
    last_current_match = -1
    while i > 0 and j > 0:
        if _message_key(previous[i - 1]) == _message_key(current[j - 1]):
            last_current_match = max(last_current_match, j - 1)
            i -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return last_current_match + 1


def _decode_snapshot(value: str | None) -> list[dict[str, str]]:
    """解析快照 JSON；旧快照的 direction 字段会被忽略。"""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return _normalise_visible([item for item in parsed if isinstance(item, dict)])


class ChatHistoryStore:
    """按联系人维护 QQ 复制消息及当前可见窗口快照。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
            self._migrate_messages_table(connection)
            # 新会话按持久身份隔离；旧按昵称查询的 HTTP 契约继续使用原表。
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS bound_message (
                    message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    seq INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    UNIQUE(session_id, conversation_id, seq));
                CREATE TABLE IF NOT EXISTS bound_snapshot (
                    session_id TEXT NOT NULL, conversation_id TEXT NOT NULL, messages_json TEXT NOT NULL,
                    PRIMARY KEY(session_id, conversation_id));
            """)

    def append_bound_visible(self, session_id: str, conversation_id: str, messages: list[dict]) -> None:
        """按出现顺序持久化复制消息；同文同秒的多个可见实例仍各有消息 ID。"""
        current = _normalise_visible(messages)
        if any(not item["sender"] or not item["timestamp"] for item in current):
            raise ValueError("QQ_MESSAGE_SOURCE_EVIDENCE_REQUIRED")
        if not current:
            return
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute("""SELECT payload_json FROM bound_message
                WHERE session_id=? AND conversation_id=? ORDER BY seq""", (session_id, conversation_id)).fetchall()
            previous = _normalise_visible([json.loads(row[0]) for row in rows])
            # 锚定持久序列而非上一屏：回看历史再返回时，旧消息不能换 ID 成为新回复。
            overlap = _longest_common_subsequence_overlap(previous, current)
            sequence = connection.execute("SELECT COALESCE(MAX(seq),0) FROM bound_message WHERE session_id=? AND conversation_id=?",
                                          (session_id, conversation_id)).fetchone()[0]
            for item in current[overlap:]:
                sequence += 1
                message_id = "qq-message-" + uuid.uuid4().hex
                payload = {**item, "messageId": message_id, "sequence": sequence,
                           "isSelf": None, "direction": "UNKNOWN", "source": "QQ_NATIVE_CLIPBOARD"}
                connection.execute("INSERT INTO bound_message VALUES(?,?,?,?,?)",
                                   (message_id, session_id, conversation_id, sequence, json.dumps(payload, ensure_ascii=False)))
            connection.execute("""INSERT INTO bound_snapshot VALUES(?,?,?)
                ON CONFLICT(session_id,conversation_id) DO UPDATE SET messages_json=excluded.messages_json""",
                (session_id, conversation_id, json.dumps(current, ensure_ascii=False)))

    def get_bound_history(self, session_id: str, conversation_id: str, after_sequence: int = 0,
                          limit: int = 100) -> list[dict]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute("""SELECT payload_json FROM bound_message
                WHERE session_id=? AND conversation_id=? AND seq>? ORDER BY seq LIMIT ?""",
                (session_id, conversation_id, after_sequence, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def bound_sequence(self, session_id: str, conversation_id: str) -> int:
        with self._lock, closing(self._connect()) as connection:
            return connection.execute("SELECT COALESCE(MAX(seq),0) FROM bound_message WHERE session_id=? AND conversation_id=?",
                                      (session_id, conversation_id)).fetchone()[0]

    @staticmethod
    def _migrate_messages_table(connection: sqlite3.Connection) -> None:
        """删除旧版或中断迁移遗留表，只保留当前消息表结构。"""
        target_columns = {
            "id", "contact_name", "sender", "timestamp", "text", "raw_text", "seq", "fingerprint", "created_at"
        }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}

        connection.execute("DROP TABLE IF EXISTS messages_legacy")
        connection.execute("DROP TABLE IF EXISTS pending_outbound")
        if columns != target_columns:
            connection.execute("DROP TABLE IF EXISTS messages")
            connection.execute(_MESSAGES_TABLE_SQL)
            connection.execute("DELETE FROM contact_snapshot")

        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_contact_time ON messages (contact_name, created_at)"
        )

    def append_visible(self, contact_name: str, messages: list[dict[str, Any]]) -> int:
        """按可见窗口差异追加复制消息，返回本次新增数。"""
        contact_name = str(contact_name or "")
        if not contact_name:
            raise ValueError("contact_name must not be empty")
        current = _normalise_visible(messages)
        if not current:
            return 0

        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT messages_json FROM contact_snapshot WHERE contact_name = ?", (contact_name,)
            ).fetchone()
            previous = _decode_snapshot(row[0]) if row else self._tail_history(connection, contact_name)
            history_tail = self._tail_history(connection, contact_name)
            overlap = max(
                _longest_common_subsequence_overlap(previous, current),
                _longest_common_subsequence_overlap(history_tail, current),
            )
            new_messages = current[overlap:]
            next_seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE contact_name = ?", (contact_name,)
            ).fetchone()[0]

            for item in new_messages:
                connection.execute(
                    "INSERT INTO messages(contact_name, sender, timestamp, text, raw_text, seq, fingerprint) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        contact_name,
                        item["sender"] or None,
                        item["timestamp"] or None,
                        item["text"],
                        item["rawText"] or None,
                        next_seq,
                        _fingerprint(
                            contact_name,
                            item["sender"],
                            item["timestamp"],
                            item["text"],
                            item["rawText"],
                        ),
                    ),
                )
                next_seq += 1

            snapshot_json = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                "INSERT INTO contact_snapshot(contact_name, messages_json) VALUES (?, ?) "
                "ON CONFLICT(contact_name) DO UPDATE SET "
                "messages_json = excluded.messages_json, updated_at = datetime('now', 'localtime')",
                (contact_name, snapshot_json),
            )
            return len(new_messages)

    def list_conversations(self, limit: int = 50) -> list[dict]:
        """返回会话的最后更新时间、消息数和最后一条正文。"""
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT m1.contact_name, MAX(m1.created_at), COUNT(*), "
                "(SELECT m2.text FROM messages m2 WHERE m2.contact_name = m1.contact_name "
                " ORDER BY m2.seq DESC LIMIT 1) "
                "FROM messages m1 GROUP BY m1.contact_name ORDER BY MAX(m1.created_at) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"contactName": row[0], "lastTime": row[1], "total": row[2], "lastText": row[3]}
            for row in rows
        ]

    def get_history(self, contact_name: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """返回最近一页历史，并按 seq 正序输出。"""
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, sender, timestamp, text, raw_text, seq, created_at "
                "FROM messages WHERE contact_name = ? ORDER BY seq DESC LIMIT ? OFFSET ?",
                (contact_name, limit, offset),
            ).fetchall()
        return [
            {
                "id": row[0],
                "sender": row[1],
                "timestamp": row[2],
                "text": row[3],
                "rawText": row[4],
                "seq": row[5],
                "createdAt": row[6],
            }
            for row in reversed(rows)
        ]

    @staticmethod
    def _tail_history(
        connection: sqlite3.Connection, contact_name: str, limit: int = 100
    ) -> list[dict[str, str]]:
        """无快照时从历史尾部恢复一个可见窗口，用于兼容旧数据库。"""
        rows = connection.execute(
            "SELECT sender, timestamp, text, raw_text FROM messages "
            "WHERE contact_name = ? ORDER BY seq DESC LIMIT ?",
            (contact_name, limit),
        ).fetchall()
        return [
            {
                "sender": str(row[0] or ""),
                "timestamp": str(row[1] or ""),
                "text": row[2],
                "rawText": str(row[3] or ""),
            }
            for row in reversed(rows)
        ]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection
