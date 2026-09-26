"""聊天记录持久化：按用户列表名字保存，并基于可见窗口差异追加消息。

设计约束：
- contact_name 使用用户列表中的原始名字（UserList.users 的 key / User.name），不做归一化。
- QQ 只能读取当前可见消息窗口；每次读取按“上一次可见窗口”和“本次可见窗口”的差异追加，
  不再使用“同联系人同方向同文本 + 60 秒”的内容去重。
- 发送成功的消息先写入 pending_outbound；下次视觉读取看到它时，再关联 command_id 并写入
  messages，保证最终消息顺序按 QQ 可见顺序排列。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any


_PENDING_OUTBOUND_TTL_SECONDS = 24 * 60 * 60

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

CREATE TABLE IF NOT EXISTS contact_snapshot (
    contact_name TEXT PRIMARY KEY,
    messages_json TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS pending_outbound (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id   TEXT    NOT NULL UNIQUE,
    contact_name TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_pending_outbound_contact
    ON pending_outbound (contact_name, text);
"""


def _fingerprint(contact_name: str, direction: str, text: str) -> str:
    """保存稳定指纹，保留给调试和后续迁移使用；当前不用于内容去重。"""
    return hashlib.sha256(f"{contact_name}|{direction}|{text}".encode("utf-8")).hexdigest()


def _normalise_visible(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把 service/facade 的可见消息转换成快照片段。"""
    result: list[dict[str, str]] = []
    for item in messages:
        direction = "out" if item.get("isSelf") else "in"
        text = str(item.get("text") or "")
        result.append({"direction": direction, "text": text})
    return result

def _message_key(message: dict[str, str]) -> tuple[str, str]:
    """窗口对齐使用的轻量身份：方向和文本。"""
    return message["direction"], message["text"].strip()


def _suffix_prefix_overlap(previous: list[dict[str, str]], current: list[dict[str, str]]) -> int:
    """返回 previous 尾部与 current 头部的最长连续重叠长度。"""
    limit = min(len(previous), len(current))
    for size in range(limit, 0, -1):
        if previous[len(previous) - size:] == current[:size]:
            return size
    return 0


def _longest_common_subsequence_overlap(previous: list[dict[str, str]], current: list[dict[str, str]]) -> int:
    """返回 current 中最后一个已见过消息的下一个下标。"""
    if not previous or not current:
        return 0
    overlap = _suffix_prefix_overlap(previous, current)
    if overlap:
        return overlap
    dp = [[0] * (len(current) + 1) for _ in range(len(previous) + 1)]
    for i in range(1, len(previous) + 1):
        prev_key = _message_key(previous[i - 1])
        for j in range(1, len(current) + 1):
            if prev_key == _message_key(current[j - 1]):
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    i, j = len(previous), len(current)
    last_current_match = -1
    while i > 0 and j > 0:
        if _message_key(previous[i - 1]) == _message_key(current[j - 1]):
            last_current_match = max(last_current_match, j - 1)
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return last_current_match + 1


def _decode_snapshot(value: str | None) -> list[dict[str, str]]:
    """解析快照 JSON；损坏数据按空快照处理。"""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    result: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        direction = item.get("direction")
        if direction not in ("in", "out"):
            continue
        result.append({"direction": direction, "text": str(item.get("text") or "")})
    return result

class ChatHistoryStore:
    """QQ 聊天记录存储，按联系人名字维护顺序和可见窗口状态。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    def mark_outbound(self, contact_name: str, text: str, command_id: str) -> None:
        """记录一条已发送但尚未在可见窗口确认的消息。"""
        contact_name = str(contact_name or "")
        if not contact_name:
            raise ValueError("contact_name must not be empty")
        text = str(text or "")
        command_id = str(command_id or "").strip()
        if not command_id:
            raise ValueError("command_id must not be empty")

        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM pending_outbound"
                " WHERE created_at <= datetime('now', 'localtime', ?)",
                (f"-{_PENDING_OUTBOUND_TTL_SECONDS} seconds",),
            )
            connection.execute(
                "INSERT INTO pending_outbound(command_id, contact_name, text)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(command_id) DO UPDATE SET"
                " contact_name = excluded.contact_name,"
                " text = excluded.text,"
                " created_at = datetime('now', 'localtime')",
                (command_id, contact_name, text),
            )

    def append_visible(self, contact_name: str, messages: list[dict[str, Any]]) -> int:
        """用本次可见消息窗口追加聊天记录，返回实际新增条数。"""
        contact_name = str(contact_name or "")
        if not contact_name:
            raise ValueError("contact_name must not be empty")

        current = _normalise_visible(messages)
        if not current:
            # 空 OCR 不覆盖已有快照，避免短暂识别失败后把历史重新写一遍。
            return 0

        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT messages_json FROM contact_snapshot WHERE contact_name = ?",
                (contact_name,),
            ).fetchone()
            previous = _decode_snapshot(row[0]) if row else self._tail_history(connection, contact_name)
            history_tail = self._tail_history(connection, contact_name)

            # 可见窗口可能被用户滚动到历史位置；同时与“上次窗口”和“历史尾部”对齐，
            # 避免把已经存在的旧消息重新追加到历史末尾。
            overlap = max(
                _longest_common_subsequence_overlap(previous, current),
                _longest_common_subsequence_overlap(history_tail, current),
            )
            new_messages = current[overlap:]

            next_seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE contact_name = ?",
                (contact_name,),
            ).fetchone()[0]

            for item in new_messages:
                direction = item["direction"]
                text = item["text"]
                command_id = None
                if direction == "out":
                    command_id = self._consume_pending(connection, contact_name, text)
                connection.execute(
                    "INSERT INTO messages(contact_name, direction, text, seq, fingerprint, command_id)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        contact_name,
                        direction,
                        text,
                        next_seq,
                        _fingerprint(contact_name, direction, text),
                        command_id,
                    ),
                )
                next_seq += 1

            snapshot_json = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                "INSERT INTO contact_snapshot(contact_name, messages_json)"
                " VALUES (?, ?)"
                " ON CONFLICT(contact_name) DO UPDATE SET"
                " messages_json = excluded.messages_json,"
                " updated_at = datetime('now', 'localtime')",
                (contact_name, snapshot_json),
            )
            return len(new_messages)
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
            {"contactName": row[0], "lastTime": row[1], "total": row[2], "lastText": row[3]}
            for row in rows
        ]

    def get_history(self, contact_name: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """返回该联系人最近的一页历史，再按 seq 升序输出。"""
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, direction, text, seq, command_id, created_at"
                " FROM messages WHERE contact_name = ?"
                " ORDER BY seq DESC LIMIT ? OFFSET ?",
                (contact_name, limit, offset),
            ).fetchall()
            rows = list(reversed(rows))
        return [
            {
                "id": row[0],
                "direction": row[1],
                "text": row[2],
                "seq": row[3],
                "commandId": row[4],
                "createdAt": row[5],
            }
            for row in rows
        ]

    def _tail_history(
        self,
        connection: sqlite3.Connection,
        contact_name: str,
        limit: int = 100,
    ) -> list[dict[str, str]]:
        """首次使用快照时，从已有历史尾部恢复上一次可见窗口，兼容旧数据库。"""
        rows = connection.execute(
            "SELECT direction, text FROM messages"
            " WHERE contact_name = ? ORDER BY seq DESC LIMIT ?",
            (contact_name, limit),
        ).fetchall()
        return [{"direction": row[0], "text": row[1]} for row in reversed(rows)]

    @staticmethod
    def _consume_pending(connection: sqlite3.Connection, contact_name: str, text: str) -> str | None:
        """把一条可见的出站消息关联到最早的待确认发送命令。"""
        row = connection.execute(
            "SELECT command_id FROM pending_outbound"
            " WHERE contact_name = ? AND text = ? ORDER BY id ASC LIMIT 1",
            (contact_name, text),
        ).fetchone()
        if row is None:
            return None
        command_id = row[0]
        connection.execute("DELETE FROM pending_outbound WHERE command_id = ?", (command_id,))
        return command_id

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection
