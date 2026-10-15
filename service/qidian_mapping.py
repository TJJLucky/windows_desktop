"""qidian 唤醒映射的本地 SQLite 存储。

服务端自己唤醒 QQ 后，把「(站点, 供应商) → 当前接待人」的结论落在这里，供客户端
在不重新唤醒的情况下复用映射。存储口径与 chat_history / command_ledger 一致：
标准库 sqlite3 + 短连接 + threading.Lock 串行化。

两张表：

- ``qidian_mapping``：主键 ``(site_id, supplier_id)``，保存**最新**映射（直接覆盖旧值）；
- ``qidian_wake_log``：每次唤醒追加一行，保存本次结果与覆盖前的旧值，用于复盘
  「某个供应商曾经对应过哪个接待人」，也是同链去重窗口的判据。

同链去重：同一 ``link_hash`` 在 ``DEDUP_WINDOW_SECONDS`` 窗口内重复请求直接复用上次
唤醒结果（调用方返回 ``reused=true``），不再打开链接、不再唤醒 QQ。
"""

from __future__ import annotations

# logging：记录覆盖写入与去重命中
import logging
# sqlite3：标准库驱动（短连接，用完即关）
import sqlite3
# threading：进程内互斥锁，串行化并发写入
import threading
# closing：with 结束自动关闭连接
from contextlib import closing
# Path：数据库文件路径
from pathlib import Path
# Any：返回结构里混排 str/int/None
from typing import Any

logger = logging.getLogger("qq_service.qidian_mapping")

# 映射来源（写死在存储层，HTTP 契约里的 source 只允许这个值）
SOURCE = "qidian_wake"

# 同链去重窗口（秒）：窗口内重复提交同一链接直接复用上次结果，不重复唤醒
DEDUP_WINDOW_SECONDS = 120.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qidian_mapping (
    site_id          TEXT    NOT NULL,
    supplier_id      TEXT    NOT NULL,
    link_hash        TEXT    NOT NULL,
    supplier_name    TEXT    NOT NULL,
    contact_name     TEXT    NOT NULL,
    qq_number        TEXT,
    target_qq_number TEXT,
    source           TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    woke_epoch       REAL    NOT NULL,
    wake_count       INTEGER NOT NULL,
    PRIMARY KEY (site_id, supplier_id)
);

CREATE INDEX IF NOT EXISTS idx_qidian_mapping_link_hash
    ON qidian_mapping (link_hash);

CREATE TABLE IF NOT EXISTS qidian_wake_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id           TEXT    NOT NULL,
    supplier_id       TEXT    NOT NULL,
    link_hash         TEXT    NOT NULL,
    supplier_name     TEXT    NOT NULL,
    contact_name      TEXT    NOT NULL,
    qq_number         TEXT,
    target_qq_number  TEXT,
    prev_contact_name TEXT,
    prev_qq_number    TEXT,
    source            TEXT    NOT NULL,
    woke_at           TEXT    NOT NULL,
    woke_epoch        REAL    NOT NULL,
    wake_count        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qidian_wake_log_link
    ON qidian_wake_log (link_hash, woke_epoch);
"""

# 映射行的统一投影：camelCase key 直接喂给 HTTP 契约模型（与 chat_history 同口径）
_MAPPING_COLUMNS = ("site_id", "supplier_id", "link_hash", "supplier_name", "contact_name",
                    "qq_number", "target_qq_number", "source", "updated_at", "wake_count")


class QidianMappingStore:
    """qidian 唤醒映射存储；写入采用「直接覆盖 + 旧值进日志」策略。"""

    def __init__(self, path: Path) -> None:
        """打开（必要时创建）数据库并建表。"""
        # 数据库文件路径
        self._path = path
        # 进程内互斥锁：sqlite 短连接本身不防跨线程竞争，必须由应用层串行化
        self._lock = threading.Lock()
        # 首次运行需要建目录
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)

    def record_wake(self, *, site_id: str, supplier_id: str, link_hash: str, supplier_name: str,
                    contact_name: str, qq_number: str | None, target_qq_number: str | None,
                    woke_at: str, woke_epoch: float) -> dict[str, Any]:
        """写入一次唤醒结果：覆盖映射行、把旧值一并留在唤醒日志里。

        :return: 覆盖后的映射行（camelCase，含累计 wakeCount）
        """
        with self._lock, closing(self._connect()) as connection, connection:
            previous = connection.execute(
                "SELECT contact_name, qq_number, wake_count FROM qidian_mapping "
                "WHERE site_id = ? AND supplier_id = ?",
                (site_id, supplier_id),
            ).fetchone()
            wake_count = (previous[2] if previous else 0) + 1
            connection.execute(
                "INSERT INTO qidian_wake_log("
                "  site_id, supplier_id, link_hash, supplier_name, contact_name, qq_number,"
                "  target_qq_number, prev_contact_name, prev_qq_number, source, woke_at, woke_epoch, wake_count"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (site_id, supplier_id, link_hash, supplier_name, contact_name, qq_number,
                 target_qq_number, previous[0] if previous else None,
                 previous[1] if previous else None, SOURCE, woke_at, woke_epoch, wake_count),
            )
            connection.execute(
                "INSERT INTO qidian_mapping("
                "  site_id, supplier_id, link_hash, supplier_name, contact_name, qq_number,"
                "  target_qq_number, source, updated_at, woke_epoch, wake_count"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(site_id, supplier_id) DO UPDATE SET "
                "  link_hash = excluded.link_hash, supplier_name = excluded.supplier_name,"
                "  contact_name = excluded.contact_name, qq_number = excluded.qq_number,"
                "  target_qq_number = excluded.target_qq_number, source = excluded.source,"
                "  updated_at = excluded.updated_at, woke_epoch = excluded.woke_epoch,"
                "  wake_count = excluded.wake_count",
                (site_id, supplier_id, link_hash, supplier_name, contact_name, qq_number,
                 target_qq_number, SOURCE, woke_at, woke_epoch, wake_count),
            )
        if previous is not None and previous[0] != contact_name:
            logger.warning(
                "qidian.mapping.overwrite site=%s supplier=%s old_contact=%r new_contact=%r old_qq=%s new_qq=%s",
                site_id, supplier_id, previous[0], contact_name, previous[1], qq_number,
            )
        else:
            logger.info("qidian.mapping.write site=%s supplier=%s contact=%r qq=%s count=%d",
                        site_id, supplier_id, contact_name, qq_number, wake_count)
        return {
            "siteId": site_id,
            "supplierId": supplier_id,
            "linkHash": link_hash,
            "supplierName": supplier_name,
            "contactName": contact_name,
            "qqNumber": qq_number,
            "targetQqNumber": target_qq_number,
            "source": SOURCE,
            "updatedAt": woke_at,
            "wakeCount": wake_count,
        }

    def recent_wake(self, link_hash: str, *, now_epoch: float,
                    window_seconds: float = DEDUP_WINDOW_SECONDS) -> dict[str, Any] | None:
        """窗口内该链接的上一次唤醒结果；没有则返回 None（调用方据此决定是否真的唤醒）。"""
        threshold = now_epoch - window_seconds
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT site_id, supplier_id, link_hash, supplier_name, contact_name, qq_number,"
                "       target_qq_number, source, woke_at, wake_count "
                "FROM qidian_wake_log WHERE link_hash = ? AND woke_epoch >= ? "
                "ORDER BY id DESC LIMIT 1",
                (link_hash, threshold),
            ).fetchone()
        if row is None:
            return None
        # 日志行直接投影成与唤醒响应同形的 camelCase 结构
        return {
            "siteId": row[0],
            "supplierId": row[1],
            "linkHash": row[2],
            "supplierName": row[3],
            "contactName": row[4],
            "qqNumber": row[5],
            "targetQqNumber": row[6],
            "source": row[7],
            "updatedAt": row[8],
            "wakeCount": row[9],
        }

    def get(self, *, site_id: str | None = None, supplier_id: str | None = None,
            link_hash: str | None = None) -> dict[str, Any] | None:
        """按 (siteId, supplierId) 或 linkHash 读回映射；没有记录返回 None。"""
        columns = ", ".join(_MAPPING_COLUMNS)
        with closing(self._connect()) as connection:
            if link_hash:
                # 同一链接可能对应过多个供应商键，取最近一次写入的那条
                row = connection.execute(
                    f"SELECT {columns} FROM qidian_mapping WHERE link_hash = ? "
                    "ORDER BY woke_epoch DESC LIMIT 1",
                    (link_hash,),
                ).fetchone()
            else:
                row = connection.execute(
                    f"SELECT {columns} FROM qidian_mapping WHERE site_id = ? AND supplier_id = ?",
                    (site_id, supplier_id),
                ).fetchone()
        if row is None:
            return None
        return {
            "siteId": row[0],
            "supplierId": row[1],
            "linkHash": row[2],
            "supplierName": row[3],
            "contactName": row[4],
            "qqNumber": row[5],
            "targetQqNumber": row[6],
            "source": row[7],
            "updatedAt": row[8],
            "wakeCount": row[9],
        }

    def _connect(self) -> sqlite3.Connection:
        """短连接工厂：每次操作新建连接、用完即关，避免长连接锁与陈旧状态。"""
        return sqlite3.connect(self._path)
