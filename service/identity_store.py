"""保存由界面证据锚定的服务会话 ID；这些 ID 不是 QQ 原生账号或好友号。"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path


class identity_store:
    """同一 QQ 进程、账号头像和联系人证据复用 ID，证据改变必须重新绑定。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS identity_session (
                    session_id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
                    evidence_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS identity_contact (
                    conversation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    source_key TEXT NOT NULL, name TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    header_name TEXT, UNIQUE(session_id, source_key));
            """)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def capture(self, snapshot: dict) -> dict[str, dict]:
        """原始重复行拒绝合并，不能把同名不同商家压缩成一个会话。"""
        source_keys = [item["sourceKey"] for item in snapshot["contacts"]]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("QQ_IDENTITY_AMBIGUOUS")
        with self._lock, closing(self._connect()) as connection, connection:
            session = connection.execute("SELECT session_id FROM identity_session WHERE source_key=?",
                                         (snapshot["sessionKey"],)).fetchone()
            session_id = session[0] if session else "qq-session-" + uuid.uuid4().hex
            connection.execute("INSERT OR IGNORE INTO identity_session VALUES(?,?,?)",
                               (session_id, snapshot["sessionKey"], json.dumps(snapshot["sessionEvidence"], ensure_ascii=False)))
            contacts = {}
            for item in snapshot["contacts"]:
                row = connection.execute("SELECT conversation_id FROM identity_contact WHERE session_id=? AND source_key=?",
                                         (session_id, item["sourceKey"])).fetchone()
                conversation_id = row[0] if row else "qq-conversation-" + uuid.uuid4().hex
                connection.execute("""INSERT INTO identity_contact VALUES(?,?,?,?,?,NULL)
                    ON CONFLICT(session_id,source_key) DO UPDATE SET evidence_json=excluded.evidence_json""",
                    (conversation_id, session_id, item["sourceKey"], item["name"], json.dumps(item["evidence"], ensure_ascii=False)))
                record = connection.execute("SELECT name,header_name FROM identity_contact WHERE conversation_id=?", (conversation_id,)).fetchone()
                contacts[conversation_id] = {
                    **item, "name": record["name"], "sessionId": session_id, "conversationId": conversation_id,
                    "identityKind": "SERVICE_UI_EVIDENCE", "verifiedHeaderName": record["header_name"],
                }
            return contacts

    def require(self, session_id: str, conversation_id: str, contact_name: str | None = None) -> dict:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute("""SELECT c.*,s.source_key AS session_key
                FROM identity_contact c JOIN identity_session s USING(session_id)
                WHERE c.session_id=? AND c.conversation_id=?""", (session_id, conversation_id)).fetchone()
        if row is None or (contact_name is not None and row["name"] != contact_name):
            raise ValueError("QQ_IDENTITY_BINDING_MISMATCH")
        return {"sessionKey": row["session_key"], "sourceKey": row["source_key"], "name": row["name"],
                "evidence": json.loads(row["evidence_json"]), "headerName": row["header_name"]}

    def record_header(self, session_id: str, conversation_id: str, header_name: str) -> None:
        if not header_name:
            raise ValueError("QQ_IDENTITY_HEADER_REQUIRED")
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT header_name FROM identity_contact WHERE session_id=? AND conversation_id=?",
                                     (session_id, conversation_id)).fetchone()
            if row is None or (row[0] is not None and row[0] != header_name):
                raise ValueError("QQ_IDENTITY_HEADER_CHANGED")
            connection.execute("UPDATE identity_contact SET header_name=? WHERE session_id=? AND conversation_id=?",
                               (header_name, session_id, conversation_id))
