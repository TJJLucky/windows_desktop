"""聊天记录持久化：把读到的（in）与发出的（out）消息写入本地 SQLite。

与 command_ledger 同约定：标准库 sqlite3、短连接、threading.Lock 串行化。
默认文件位于 <state-dir>/qq-chat-history.sqlite3（独立文件，避免账本锁竞争）。

设计要点：
- 单表 messages，冗余存储 contact_name（不建联系人外键表）：OCR 识别出的名字可能漂移/改名，
  冗余字段保证记录不因"联系人改名"而断链；
- 近窗查重：60 秒内同联系人、同方向、同文本的重复消息跳过（视觉重复读取会产生完全相同的条目）；
- seq 按联系人递增：同一联系人的消息有稳定顺序，查询按 seq 升序即时间顺序；
- fingerprint：联系人|方向|文本 的 sha256，作为重复判定的稳定指纹。
"""

# 延迟求值类型注解
from __future__ import annotations

# hashlib：计算消息指纹（sha256）
import hashlib
# sqlite3：标准库 SQLite 驱动（短连接模式）
import sqlite3
# threading：进程内互斥锁，串行化并发写入
import threading
# closing：with 块结束自动关闭连接
from contextlib import closing
# Path：数据库文件路径
from pathlib import Path

# 去重窗口：60 秒内同联系人同方向同文本视为重复（视觉轮询会产生重复读取）
_DEDUP_WINDOW_SECONDS = 60

# 建表 SQL：messages 单表 + 两个查询索引
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,          -- 自增主键
    contact_name TEXT    NOT NULL,                            -- 联系人名（冗余存储，见模块 docstring）
    direction    TEXT    NOT NULL CHECK (direction IN ('in', 'out')),  -- 消息方向：in 对方发来 / out 我方发出
    text         TEXT    NOT NULL,                            -- 消息文本
    seq          INTEGER NOT NULL,                            -- 该联系人维度下的递增序号
    fingerprint  TEXT    NOT NULL,                            -- 去重指纹：sha256(联系人|方向|文本)
    command_id   TEXT,                                        -- 我方经命令发送时关联的幂等键（可空）
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),  -- 入库时间（本地时间）
    CONSTRAINT uq_contact_seq UNIQUE (contact_name, seq)      -- 联系人 + 序号唯一，防重复插入
);
-- 按联系人+时间查询（会话列表）的索引
CREATE INDEX IF NOT EXISTS idx_messages_contact_time
    ON messages (contact_name, created_at);
-- 按联系人+方向+文本+时间查询（查重）的索引
CREATE INDEX IF NOT EXISTS idx_messages_contact_text_time
    ON messages (contact_name, direction, text, created_at);
"""


def _fingerprint(contact_name: str, direction: str, text: str) -> str:
    """计算消息指纹：联系人|方向|文本 拼接后 sha256 十六进制。"""
    return hashlib.sha256(f"{contact_name}|{direction}|{text}".encode("utf-8")).hexdigest()


class ChatHistoryStore:
    """QQ 聊天记录存储（单文件 SQLite，短连接 + 锁串行化）。"""

    def __init__(self, path: Path, dedup_window: int = _DEDUP_WINDOW_SECONDS) -> None:
        """打开（必要时创建）聊天记录库，并建表。"""
        # 数据库文件路径
        self._path = path
        # 去重窗口秒数（测试可注入更短窗口验证去重）
        self._dedup_window = dedup_window
        # 进程内互斥锁：sqlite 短连接不防跨线程竞争，必须应用层串行化
        self._lock = threading.Lock()
        # 确保目录存在（首次运行建目录）
        path.parent.mkdir(parents=True, exist_ok=True)
        # 建表 + 启用 WAL（写不阻塞读，读不阻塞写，适合"高频追加 + 偶发查询"）
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    def append(self, contact_name: str, direction: str, text: str, command_id: str | None = None) -> bool:
        """写一条消息：近窗查重 → seq 递增 → 插入。

        :return: True=已写入；False=去重窗口内同联系人同方向同文本（重复读取）已跳过
        """
        # 方向校验：只允许 in/out，拒绝脏数据入库
        if direction not in ("in", "out"):
            raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
        # 文本归一化：None 当空串处理（防御 OCR 空结果）
        text = str(text or "")
        with self._lock, closing(self._connect()) as connection, connection:
            # 近窗查重：同联系人、同方向、同文本，且入库时间在窗口内 → 视为重复读取
            duplicate = connection.execute(
                "SELECT id FROM messages"
                " WHERE contact_name = ? AND direction = ? AND text = ?"
                " AND created_at > datetime('now', 'localtime', ?)"
                " ORDER BY id DESC LIMIT 1",
                (contact_name, direction, text, f"-{self._dedup_window} seconds"),
            ).fetchone()
            if duplicate is not None:
                # 命中重复：不写入，返回 False 告知调用方被去重
                return False
            # 计算该联系人下一个序号：当前最大 seq + 1（联系人维度连续递增）
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE contact_name = ?",
                (contact_name,),
            ).fetchone()[0]
            # 插入记录（指纹由调用方上下文计算）
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
            # 子查询取每个联系人的最后一条文本；GROUP BY 按联系人聚合
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
        # 行 → 字典（蛇形转 JSON 键名）
        return [
            {"contactName": r[0], "lastTime": r[1], "total": r[2], "lastText": r[3]}
            for r in rows
        ]

    def get_history(self, contact_name: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """单联系人历史消息（按会话序号升序分页）。"""
        with self._lock, closing(self._connect()) as connection:
            # 按 seq 升序取一页（seq 即该联系人的时间顺序）
            rows = connection.execute(
                "SELECT id, direction, text, seq, command_id, created_at"
                " FROM messages WHERE contact_name = ?"
                " ORDER BY seq ASC LIMIT ? OFFSET ?",
                (contact_name, limit, offset),
            ).fetchall()
        # 行 → 契约字典（commandId / createdAt 为对外键名）
        return [
            {"id": r[0], "direction": r[1], "text": r[2], "seq": r[3],
             "commandId": r[4], "createdAt": r[5]}
            for r in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        """短连接工厂：每次操作新建连接，用完即关。"""
        return sqlite3.connect(self._path)
