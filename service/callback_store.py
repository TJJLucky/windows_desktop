"""订阅、命令排队和回调 outbox 的持久投递账本，不保存询价业务状态。"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .command_ledger import CommandConflictError


IDENTITY_INVALIDATION_ERRORS = frozenset({
    "QQ_IDENTITY_SESSION_CHANGED", "QQ_IDENTITY_HEADER_CHANGED", "QQ_IDENTITY_HEADER_MISMATCH",
    "QQ_IDENTITY_CONTACT_CHANGED", "QQ_IDENTITY_CHANGED_DURING_READ", "QQ_IDENTITY_CHANGED_BEFORE_SEND",
})


class subscription_conflict(RuntimeError):
    """订阅标识被不同身份或消费方复用。"""


class callback_store:
    """与发送账本使用同一 SQLite 文件，发送终态和 outbox 在一个事务中提交。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        with closing(self._connect()) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS qq_callback_subscription (
                    subscription_id TEXT PRIMARY KEY, consumer_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, command_id TEXT,
                    after_sequence INTEGER NOT NULL, callback_url TEXT NOT NULL,
                    callback_token TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS qq_callback_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    subscription_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    UNIQUE(subscription_id,source_id)
                );
                CREATE TABLE IF NOT EXISTS qq_deferred_command (
                    command_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL,
                    consumer_id TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qq_command_cancellation (
                    command_id TEXT PRIMARY KEY, consumer_id TEXT NOT NULL
                );
            """)
            # 进程退出前已经跨过执行边界的命令由旧账本迁为 UNKNOWN；只补事件，绝不重发。
            for row in connection.execute("""
                SELECT d.command_id,c.status,c.result_json FROM qq_deferred_command d
                JOIN qq_send_command c ON c.command_id=d.command_id
                WHERE c.status IN ('SUCCEEDED','FAILED','EFFECT_UNKNOWN','CANCELLED')
            """).fetchall():
                self._record_command_event(connection, row[0], row[1], _decode(row[2]))

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def subscribe(self, payload: dict) -> None:
        immutable = (payload["consumerId"], payload["sessionId"], payload["conversationId"],
                     payload["eventTypes"][0], payload.get("commandId"))
        with self._lock, closing(self._connect()) as connection, connection:
            previous = connection.execute(
                "SELECT * FROM qq_callback_subscription WHERE subscription_id=?", (payload["subscriptionId"],)
            ).fetchone()
            if previous is not None:
                existing = tuple(previous[key] for key in (
                    "consumer_id", "session_id", "conversation_id", "event_type", "command_id"
                ))
                if existing != immutable:
                    raise subscription_conflict("QQ_SUBSCRIPTION_ID_REUSED")
                connection.execute("""
                    UPDATE qq_callback_subscription SET callback_url=?,callback_token=?,
                        after_sequence=min(after_sequence,?),active=1 WHERE subscription_id=?
                """, (payload["callbackUrl"], payload["callbackToken"], payload["afterSequence"], payload["subscriptionId"]))
                # 端口改变后立即重投未确认事件，不等待旧端口的退避时间。
                connection.execute("UPDATE qq_callback_outbox SET next_attempt=0 WHERE subscription_id=? AND acknowledged=0",
                                   (payload["subscriptionId"],))
            else:
                connection.execute("""
                    INSERT INTO qq_callback_subscription(subscription_id,consumer_id,session_id,conversation_id,
                        event_type,command_id,after_sequence,callback_url,callback_token) VALUES(?,?,?,?,?,?,?,?,?)
                """, (payload["subscriptionId"], *immutable, payload["afterSequence"],
                      payload["callbackUrl"], payload["callbackToken"]))
            if payload.get("commandId"):
                command = connection.execute("SELECT status,result_json FROM qq_send_command WHERE command_id=?",
                                             (payload["commandId"],)).fetchone()
                if command is not None and command["status"] not in {"RUNNING", "QUEUED"}:
                    self._record_command_event(connection, payload["commandId"], command["status"], _decode(command["result_json"]))
                    acknowledged = connection.execute("SELECT acknowledged FROM qq_callback_outbox WHERE subscription_id=? AND source_id=?",
                                                      (payload["subscriptionId"], payload["commandId"])).fetchone()
                    if acknowledged is not None and acknowledged[0]:
                        connection.execute("UPDATE qq_callback_subscription SET active=0 WHERE subscription_id=?", (payload["subscriptionId"],))

    def unsubscribe(self, subscription_id: str, consumer_id: str) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT consumer_id FROM qq_callback_subscription WHERE subscription_id=?", (subscription_id,)).fetchone()
            if row is None or row[0] != consumer_id:
                raise subscription_conflict("QQ_SUBSCRIPTION_OWNER_MISMATCH")
            connection.execute("UPDATE qq_callback_subscription SET active=0 WHERE subscription_id=?", (subscription_id,))

    def active_message_subscriptions(self) -> list[dict]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT session_id,conversation_id,after_sequence,subscription_id
                FROM qq_callback_subscription WHERE active=1 AND event_type='message.received'
                ORDER BY session_id,conversation_id,subscription_id
            """).fetchall()
        groups = {}
        for row in rows:
            group = groups.setdefault((row[0], row[1]), {
                "sessionId": row[0], "conversationId": row[1], "afterSequence": row[2], "subscriptionIds": [],
            })
            group["afterSequence"] = min(group["afterSequence"], row[2])
            group["subscriptionIds"].append(row[3])
        return list(groups.values())

    def publish_identity_invalidated(self, session_id: str, conversation_id: str, error_code: str) -> None:
        """明确身份失效单独入账；停止继续观察，等待 Cloud 确认并处理原绑定。"""

        if error_code not in IDENTITY_INVALIDATION_ERRORS:
            raise ValueError("仅明确身份失效可终止订阅")
        with self._lock, closing(self._connect()) as connection, connection:
            records = connection.execute("""
                SELECT * FROM qq_callback_subscription WHERE active=1 AND event_type='message.received'
                  AND session_id=? AND conversation_id=?
            """, (session_id, conversation_id)).fetchall()
            for record in records:
                self._append_event(connection, record, "identity-invalidated:" + error_code, {
                    "eventType": "conversation.invalidated", "reason": error_code, "errorCode": error_code,
                })
                connection.execute("UPDATE qq_callback_subscription SET active=0 WHERE subscription_id=?",
                                   (record["subscription_id"],))

    def publish_received_message(self, session_id: str, conversation_id: str, message: dict) -> None:
        """消息身份与序号来自持久采集层；同文不同 ID 独立投递，重复 ID 只入账一次。"""

        if (not isinstance(message.get("messageId"), str) or not message["messageId"]
                or type(message.get("sequence")) is not int or message["sequence"] <= 0
                or not (message.get("isSelf") is False or (
                    message.get("isSelf") is None and message.get("direction") == "UNKNOWN"
                    and isinstance(message.get("sender"), str) and bool(message["sender"])
                    and message.get("source") == "QQ_NATIVE_CLIPBOARD"
                ))):
            raise ValueError("来信缺少稳定身份、序号或明确的发送方归属")
        with self._lock, closing(self._connect()) as connection, connection:
            records = connection.execute("""
                SELECT * FROM qq_callback_subscription WHERE active=1 AND event_type='message.received'
                  AND session_id=? AND conversation_id=? AND after_sequence<?
            """, (session_id, conversation_id, message["sequence"])).fetchall()
            for record in records:
                self._append_event(connection, record, message["messageId"], {
                    "sequence": message["sequence"], "message": message,
                })

    def enqueue(self, request: dict) -> dict:
        material = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(material.encode()).hexdigest()
        with self._lock, closing(self._connect()) as connection, connection:
            previous = connection.execute("SELECT payload_hash FROM qq_send_command WHERE command_id=?", (request["commandId"],)).fetchone()
            if previous is not None:
                if previous[0] != digest:
                    raise CommandConflictError("QQ_COMMAND_ID_REUSED")
                return self._command_response(connection, request["commandId"])
            subscription = connection.execute("SELECT * FROM qq_callback_subscription WHERE subscription_id=?",
                                              (request["subscriptionId"],)).fetchone()
            if (subscription is None or not subscription["active"] or subscription["event_type"] != "command.completed"
                    or subscription["command_id"] != request["commandId"] or subscription["session_id"] != request["sessionId"]
                    or subscription["conversation_id"] != request["conversationId"]):
                raise subscription_conflict("QQ_COMMAND_SUBSCRIPTION_REQUIRED")
            cancelled = connection.execute("SELECT consumer_id FROM qq_command_cancellation WHERE command_id=?", (request["commandId"],)).fetchone()
            if cancelled is not None:
                raise subscription_conflict("QQ_COMMAND_CANCELLED")
            connection.execute("INSERT INTO qq_send_command(command_id,payload_hash,status) VALUES(?,?,'QUEUED')", (request["commandId"], digest))
            connection.execute("INSERT INTO qq_deferred_command(command_id,subscription_id,consumer_id,payload_json) VALUES(?,?,?,?)",
                               (request["commandId"], request["subscriptionId"], subscription["consumer_id"], material))
            return self._command_response(connection, request["commandId"])

    def next_command(self) -> dict | None:
        """只把确定尚未开始的 QUEUED 命令跨入执行边界。"""

        with self._lock, closing(self._connect()) as connection, connection:
            record = connection.execute("""
                SELECT d.command_id,d.payload_json FROM qq_deferred_command d
                JOIN qq_send_command c ON c.command_id=d.command_id
                WHERE c.status='QUEUED' ORDER BY d.rowid LIMIT 1
            """).fetchone()
            if record is None:
                return None
            connection.execute("UPDATE qq_send_command SET status='RUNNING' WHERE command_id=? AND status='QUEUED'", (record[0],))
            return json.loads(record[1])

    def resolve_command(self, command_id: str, status: str, result: dict) -> None:
        if status not in {"SUCCEEDED", "FAILED", "EFFECT_UNKNOWN"}:
            raise ValueError("命令完成状态无效")
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("UPDATE qq_send_command SET status=?,result_json=? WHERE command_id=? AND status='RUNNING'",
                               (status, json.dumps(result, ensure_ascii=False), command_id))
            self._record_command_event(connection, command_id, status, result)

    def cancel_command(self, command_id: str, consumer_id: str) -> str:
        with self._lock, closing(self._connect()) as connection, connection:
            command = connection.execute("SELECT consumer_id FROM qq_deferred_command WHERE command_id=?", (command_id,)).fetchone()
            if command is not None and command[0] != consumer_id:
                raise subscription_conflict("QQ_COMMAND_OWNER_MISMATCH")
            existing_cancel = connection.execute("SELECT consumer_id FROM qq_command_cancellation WHERE command_id=?", (command_id,)).fetchone()
            if existing_cancel is not None and existing_cancel[0] != consumer_id:
                raise subscription_conflict("QQ_COMMAND_OWNER_MISMATCH")
            connection.execute("INSERT OR IGNORE INTO qq_command_cancellation(command_id,consumer_id) VALUES(?,?)", (command_id, consumer_id))
            row = connection.execute("SELECT status FROM qq_send_command WHERE command_id=?", (command_id,)).fetchone()
            if row is None:
                return "CANCELLED"
            if command is None:
                raise subscription_conflict("QQ_COMMAND_NOT_OWNED_BY_CONSUMER")
            if row[0] == "QUEUED":
                result = {"ok": False, "sent": False, "error": "QQ_COMMAND_CANCELLED"}
                connection.execute("UPDATE qq_send_command SET status='CANCELLED',result_json=? WHERE command_id=?", (json.dumps(result), command_id))
                self._record_command_event(connection, command_id, "FAILED", result)
                return "CANCELLED"
            return row[0]

    def get_command(self, command_id: str) -> dict | None:
        with self._lock, closing(self._connect()) as connection:
            return self._command_response(connection, command_id)

    def _command_response(self, connection, command_id):
        row = connection.execute("""
            SELECT c.status,c.result_json,d.subscription_id FROM qq_send_command c
            JOIN qq_deferred_command d ON c.command_id=d.command_id WHERE c.command_id=?
        """, (command_id,)).fetchone()
        if row is None:
            return None
        status = {"QUEUED": "ACCEPTED", "CANCELLED": "FAILED"}.get(row[0], row[0])
        return {"commandId": command_id, "subscriptionId": row[2], "status": status, "result": _decode(row[1])}

    def _record_command_event(self, connection, command_id, status, result):
        records = connection.execute("SELECT * FROM qq_callback_subscription WHERE event_type='command.completed' AND command_id=?", (command_id,)).fetchall()
        for record in records:
            self._append_event(connection, record, command_id, {
                "commandId": command_id, "status": "FAILED" if status == "CANCELLED" else status,
                "result": result or {"ok": False, "sent": False, "error": "QQ_SEND_RESULT_UNKNOWN"},
            })

    def _append_event(self, connection, subscription, source_id, detail):
        existing = connection.execute("SELECT event_id FROM qq_callback_outbox WHERE subscription_id=? AND source_id=?",
                                      (subscription["subscription_id"], source_id)).fetchone()
        if existing is not None:
            return
        event_id = uuid.uuid4().hex
        cursor = connection.execute("INSERT INTO qq_callback_outbox(event_id,subscription_id,source_id,payload_json) VALUES(?,?,?,'{}')",
                                    (event_id, subscription["subscription_id"], source_id))
        payload = {"schemaVersion": 1, "subscriptionId": subscription["subscription_id"], "eventId": event_id,
                   "eventType": subscription["event_type"], "sessionId": subscription["session_id"],
                   "conversationId": subscription["conversation_id"], "sequence": cursor.lastrowid,
                   "occurredAt": datetime.now(timezone.utc).isoformat(), **detail}
        connection.execute("UPDATE qq_callback_outbox SET payload_json=? WHERE event_id=?", (json.dumps(payload, ensure_ascii=False), event_id))

    def pending_events(self, now: float | None = None) -> list[dict]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT e.event_id,e.payload_json,e.attempts,s.callback_url,s.callback_token
                FROM qq_callback_outbox e JOIN qq_callback_subscription s ON e.subscription_id=s.subscription_id
                WHERE e.acknowledged=0 AND e.next_attempt<=? ORDER BY e.id LIMIT 100
            """, (time.time() if now is None else now,)).fetchall()
        return [dict(row) for row in rows]

    def acknowledge(self, event_id: str) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("UPDATE qq_callback_outbox SET acknowledged=1 WHERE event_id=?", (event_id,))
            connection.execute("""
                UPDATE qq_callback_subscription SET active=0 WHERE event_type='command.completed'
                  AND subscription_id=(SELECT subscription_id FROM qq_callback_outbox WHERE event_id=?)
            """, (event_id,))

    def retry_later(self, event_id: str, attempts: int) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("UPDATE qq_callback_outbox SET attempts=attempts+1,next_attempt=? WHERE event_id=? AND acknowledged=0",
                               (time.time() + min(60.0, 2 ** min(attempts, 6)), event_id))


def _decode(value):
    return json.loads(value) if value is not None else None
