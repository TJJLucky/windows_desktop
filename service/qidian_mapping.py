"""Scoped verified QQ conversations. Legacy unverified tables are retained but never reused."""
from __future__ import annotations
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qidian_verified_conversation (
 account_scope TEXT NOT NULL, qq_number TEXT NOT NULL, contact_name TEXT NOT NULL,
 verified_at TEXT NOT NULL, updated_at TEXT NOT NULL, wake_count INTEGER NOT NULL,
 PRIMARY KEY(account_scope, qq_number));
CREATE TABLE IF NOT EXISTS qidian_verified_supplier (
 account_scope TEXT NOT NULL, site_id TEXT NOT NULL, supplier_id TEXT NOT NULL,
 supplier_name TEXT NOT NULL, link_hash TEXT NOT NULL, qq_number TEXT NOT NULL,
 updated_at TEXT NOT NULL, PRIMARY KEY(account_scope, site_id, supplier_id));
CREATE TABLE IF NOT EXISTS qidian_verified_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_scope TEXT NOT NULL,
 site_id TEXT NOT NULL, supplier_id TEXT NOT NULL, payload TEXT NOT NULL,
 previous TEXT, created_at TEXT NOT NULL);
"""

class QidianMappingStore:
    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)

    def _connect(self):
        connection = sqlite3.connect(self._path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def conversation(self, account_scope: str, qq_number: str):
        with closing(self._connect()) as connection:
            row = connection.execute('SELECT * FROM qidian_verified_conversation WHERE account_scope=? AND qq_number=?', (account_scope, qq_number)).fetchone()
        return dict(row) if row else None

    def invalidate(self, account_scope: str, qq_number: str):
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute('DELETE FROM qidian_verified_conversation WHERE account_scope=? AND qq_number=?', (account_scope, qq_number))

    def record_verified(self, *, account_scope: str, site_id: str, supplier_id: str,
                        supplier_name: str, result: dict, audit: dict | None = None):
        number = result['targetQqNumber']
        if result.get('qqNumber') != number or not result.get('qqNumberVerified') or not result.get('activeStable'):
            raise ValueError('QQ_QIDIAN_SESSION_NOT_STABLE: 未验证的号码禁止保存')
        now = result['wokeAt']
        with self._lock, closing(self._connect()) as connection, connection:
            previous = connection.execute('SELECT * FROM qidian_verified_supplier WHERE account_scope=? AND site_id=? AND supplier_id=?', (account_scope, site_id, supplier_id)).fetchone()
            old = connection.execute('SELECT * FROM qidian_verified_conversation WHERE account_scope=? AND qq_number=?', (account_scope, number)).fetchone()
            count = (old['wake_count'] if old else 0) + 1
            verified_at = now
            connection.execute('INSERT OR REPLACE INTO qidian_verified_conversation VALUES (?,?,?,?,?,?)', (account_scope, number, result['contactName'], verified_at, now, count))
            connection.execute('INSERT OR REPLACE INTO qidian_verified_supplier VALUES (?,?,?,?,?,?,?)', (account_scope, site_id, supplier_id, supplier_name, result['linkHash'], number, now))
            connection.execute('INSERT INTO qidian_verified_log(account_scope,site_id,supplier_id,payload,previous,created_at) VALUES (?,?,?,?,?,?)', (account_scope, site_id, supplier_id, json.dumps({**result, 'audit': audit}, ensure_ascii=False), json.dumps({'supplier': dict(previous) if previous else None, 'conversation': dict(old) if old else None}, ensure_ascii=False), now))
        return result

    def get(self, *, account_scope: str, site_id=None, supplier_id=None, link_hash=None):
        sql = '''SELECT s.*, c.contact_name, c.verified_at, c.wake_count FROM qidian_verified_supplier s
                 JOIN qidian_verified_conversation c ON s.account_scope=c.account_scope AND s.qq_number=c.qq_number
                 WHERE s.account_scope=? AND '''
        args = [account_scope]
        if link_hash:
            sql += 's.link_hash=? ORDER BY s.updated_at DESC LIMIT 1'
            args.append(link_hash)
        else:
            sql += 's.site_id=? AND s.supplier_id=?'
            args.extend([site_id, supplier_id])
        with closing(self._connect()) as connection:
            row = connection.execute(sql, args).fetchone()
        if not row:
            return None
        return dict(accountScope=account_scope, siteId=row['site_id'], supplierId=row['supplier_id'], supplierName=row['supplier_name'], linkHash=row['link_hash'], contactName=row['contact_name'], qqNumber=row['qq_number'], targetQqNumber=row['qq_number'], source='qidian_wake', updatedAt=row['updated_at'], wakeCount=row['wake_count'])
