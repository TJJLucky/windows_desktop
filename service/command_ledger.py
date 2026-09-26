"""本地发送命令去重账本。"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Literal


CommandStatus = Literal["RUNNING", "SUCCEEDED", "FAILED", "EFFECT_UNKNOWN"]


class CommandConflictError(RuntimeError):
    """同一个 commandId 被不同消息体复用。"""


class CommandLedger:
    """只记录命令身份和状态；启动后不重放结果未知的发送。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS qq_send_command (
                    command_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT
                )
                """
            )
            connection.execute("UPDATE qq_send_command SET status = 'EFFECT_UNKNOWN' WHERE status = 'RUNNING'")

    def begin(self, command_id: str, contact_name: str, text: str) -> tuple[bool, CommandStatus, dict[str, Any] | None]:
        payload_hash = _payload_hash(contact_name, text)
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT payload_hash, status, result_json FROM qq_send_command WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO qq_send_command(command_id, payload_hash, status) VALUES (?, ?, 'RUNNING')",
                    (command_id, payload_hash),
                )
                return True, "RUNNING", None
            if row[0] != payload_hash:
                raise CommandConflictError("QQ_COMMAND_ID_REUSED")
            return False, row[1], _decode_result(row[2])

    def resolve(self, command_id: str, status: Literal["SUCCEEDED", "FAILED"], result: dict[str, Any]) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE qq_send_command SET status = ?, result_json = ? WHERE command_id = ?",
                (status, json.dumps(result, ensure_ascii=False, separators=(",", ":")), command_id),
            )

    def get(self, command_id: str) -> tuple[CommandStatus, dict[str, Any] | None] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status, result_json FROM qq_send_command WHERE command_id = ?", (command_id,)
            ).fetchone()
        return None if row is None else (row[0], _decode_result(row[1]))

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)


def _payload_hash(contact_name: str, text: str) -> str:
    material = json.dumps(
        {"contactName": contact_name, "text": text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _decode_result(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else None
