"""本地发送命令去重账本。

职责：保证「相同 commandId 只执行一次」的幂等语义。
- 首次收到某 commandId → 记 RUNNING 并放行执行；
- 再次收到相同 commandId + 相同消息体（联系人+文本的哈希一致）→ 幂等返回既有状态，不重发；
- 相同 commandId + 不同消息体 → 抛 CommandConflictError（调用方转为 409）；
- 服务重启时，把所有还挂着 RUNNING 的记录转为 EFFECT_UNKNOWN：表示"结果未知、不会自动重发"，
  由调用方决定是否重试。

存储约定与 chat_history 一致：标准库 sqlite3、短连接、threading.Lock 串行化。
"""

# 延迟求值类型注解：允许前向引用、加快 import
from __future__ import annotations

# closing：with 块结束时自动关闭数据库连接（短连接模式）
from contextlib import closing
# hashlib：计算消息体指纹（sha256），用于幂等比对
import hashlib
# json：结果序列化/反序列化（结果存 JSON 文本列）
import json
# logging：记录命令状态迁移
import logging
# Path：数据库文件路径
from pathlib import Path
# sqlite3：标准库 SQLite 驱动（短连接，用完即关）
import sqlite3
# threading：进程内互斥锁，串行化并发写入（服务是多线程处理请求的）
import threading
# Any：宽松结果类型；Literal：命令状态的字面量约束
from typing import Any, Literal

logger = logging.getLogger("qq_service.command_ledger")


# 命令状态机：RUNNING(执行中) → SUCCEEDED(成功) / FAILED(失败)；重启后 RUNNING → EFFECT_UNKNOWN(结果未知)
CommandStatus = Literal["RUNNING", "SUCCEEDED", "FAILED", "EFFECT_UNKNOWN"]


class CommandConflictError(RuntimeError):
    """同一个 commandId 被不同消息体复用（幂等键冲突）。"""
    # 继承 RuntimeError 便于调用方单独捕获；消息体由外部填充（如 QQ_COMMAND_ID_REUSED）


class CommandLedger:
    """只记录命令身份和状态；启动后不重放结果未知的发送。"""

    def __init__(self, path: Path) -> None:
        """打开（必要时创建）账本数据库，并做一次启动态迁移。"""
        # 数据库文件路径
        self._path = path
        # 进程内互斥锁：sqlite 短连接本身不防跨线程竞争，必须由应用层串行化
        self._lock = threading.Lock()
        # 确保数据库所在目录存在（首次运行需要建目录）
        path.parent.mkdir(parents=True, exist_ok=True)
        # 建表 + 迁移，事务内完成（with connection 提交/回滚）
        with closing(self._connect()) as connection, connection:
            # 建表（幂等）：主键 command_id、消息体哈希、当前状态、结果 JSON
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
            # 启动迁移：任何上次运行遗留的 RUNNING（进程崩溃/被杀导致）都转为 EFFECT_UNKNOWN，
            # 语义是"发送结果不确定"，绝不自动重发（避免重复给用户发消息）
            cursor = connection.execute("UPDATE qq_send_command SET status = 'EFFECT_UNKNOWN' WHERE status = 'RUNNING'")
            if cursor.rowcount:
                logger.warning("ledger.startup_migration running_to_effect_unknown count=%d", cursor.rowcount)

    def begin(self, command_id: str, contact_name: str, text: str) -> tuple[bool, CommandStatus, dict[str, Any] | None]:
        """申请执行命令：决定「本次是否真正执行」。

        :return: (execute, status, result)
                 execute=True  → 新命令，已记 RUNNING，调用方应执行发送；
                 execute=False → 幂等命中（同 ID 同消息体），返回既有状态与结果，不重发。
        :raises CommandConflictError: 同 ID 不同消息体
        """
        # 计算消息体指纹：联系人 + 文本（JSON 规范化后 sha256）
        payload_hash = _payload_hash(contact_name, text)
        with self._lock, closing(self._connect()) as connection, connection:
            # 查既有记录
            row = connection.execute(
                "SELECT payload_hash, status, result_json FROM qq_send_command WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None:
                # 首次出现：插入 RUNNING 记录并放行执行
                connection.execute(
                    "INSERT INTO qq_send_command(command_id, payload_hash, status) VALUES (?, ?, 'RUNNING')",
                    (command_id, payload_hash),
                )
                logger.info("ledger.begin command_id=%s status=RUNNING", command_id)
                return True, "RUNNING", None
            if row[0] != payload_hash:
                # 幂等键被不同消息体复用 → 冲突（调用方转 409）
                logger.warning("ledger.conflict command_id=%s", command_id)
                raise CommandConflictError("QQ_COMMAND_ID_REUSED")
            # 幂等命中：返回既有状态与结果，不执行
            logger.info("ledger.idempotent command_id=%s status=%s", command_id, row[1])
            return False, row[1], _decode_result(row[2])

    def resolve(self, command_id: str, status: Literal["SUCCEEDED", "FAILED"], result: dict[str, Any]) -> None:
        """执行结束后把命令落到终态（SUCCEEDED / FAILED），并保存执行结果。"""
        with self._lock, closing(self._connect()) as connection, connection:
            # 更新状态 + 结果 JSON（压缩格式减小体积）
            connection.execute(
                "UPDATE qq_send_command SET status = ?, result_json = ? WHERE command_id = ?",
                (status, json.dumps(result, ensure_ascii=False, separators=(",", ":")), command_id),
            )
            logger.info("ledger.resolve command_id=%s status=%s", command_id, status)

    def get(self, command_id: str) -> tuple[CommandStatus, dict[str, Any] | None] | None:
        """查询命令当前状态与结果；不存在返回 None。"""
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status, result_json FROM qq_send_command WHERE command_id = ?", (command_id,)
            ).fetchone()
        # 无记录 → None；有记录 → (状态, 解码后的结果)
        return None if row is None else (row[0], _decode_result(row[1]))

    def _connect(self) -> sqlite3.Connection:
        """短连接工厂：每次操作都新建连接，用完即关，避免长连接锁与陈旧状态。"""
        return sqlite3.connect(self._path)


def _payload_hash(contact_name: str, text: str) -> str:
    """计算发送消息体的规范化指纹：联系人 + 文本按固定键序 JSON 序列化后 sha256。"""
    # sort_keys=True 保证键序稳定（同内容必得同哈希）；separators 压缩体积
    material = json.dumps(
        {"contactName": contact_name, "text": text},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    # 返回十六进制摘要
    return hashlib.sha256(material).hexdigest()


def _decode_result(value: str | None) -> dict[str, Any] | None:
    """把结果 JSON 文本安全地解码为 dict；空值或非 dict 一律返回 None。"""
    # 空值（从未写结果）直接返回 None
    if not value:
        return None
    # 解析 JSON
    parsed = json.loads(value)
    # 防御：数据库里出现非对象 JSON 时按无结果处理
    return parsed if isinstance(parsed, dict) else None
