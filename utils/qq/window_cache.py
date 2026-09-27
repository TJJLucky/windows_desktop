"""窗口状态 SQLite 缓存。

只保存窗口元数据，用于判断是否可以跳过 L3 窗口整理；不缓存截图、OCR 或用户列表。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS window_cache (
    hwnd INTEGER PRIMARY KEY,
    pid INTEGER,
    title TEXT NOT NULL,
    left INTEGER NOT NULL,
    top INTEGER NOT NULL,
    right INTEGER NOT NULL,
    bottom INTEGER NOT NULL,
    minimized INTEGER NOT NULL,
    maximized INTEGER NOT NULL,
    topmost INTEGER NOT NULL,
    dpi INTEGER NOT NULL DEFAULT 0,
    last_checked_at REAL NOT NULL,
    last_full_refresh_at REAL NOT NULL
);
"""


def default_window_cache_path() -> Path:
    """返回默认窗口缓存数据库路径。"""
    configured = os.environ.get("QQ_WINDOW_CACHE_DB")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "price-agent-qq-service" / "state" / "qq-window-cache.sqlite3"


@dataclass(frozen=True)
class WindowSnapshot:
    """一个窗口的可比较状态。"""

    hwnd: int
    pid: int | None
    title: str
    rect: tuple[int, int, int, int]
    minimized: bool
    maximized: bool
    topmost: bool
    dpi: int
    last_checked_at: float
    last_full_refresh_at: float

    def matches(
        self,
        *,
        pid: int | None,
        title: str,
        rect: tuple[int, int, int, int],
        minimized: bool,
        maximized: bool,
        topmost: bool,
        dpi: int,
    ) -> bool:
        return (
            self.pid == pid
            and self.title == title
            and self.rect == rect
            and self.minimized == minimized
            and self.maximized == maximized
            and self.topmost == topmost
            and self.dpi == dpi
        )

    def full_refresh_due(self, now: float, ttl_seconds: float) -> bool:
        return now - self.last_full_refresh_at >= ttl_seconds


class WindowStateCache:
    """SQLite 窗口状态缓存，短连接 + 进程内锁。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    def get(self, hwnd: int) -> WindowSnapshot | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT hwnd, pid, title, left, top, right, bottom, minimized, maximized, topmost, dpi,"
                "       last_checked_at, last_full_refresh_at"
                " FROM window_cache WHERE hwnd = ?",
                (hwnd,),
            ).fetchone()
        if row is None:
            return None
        return WindowSnapshot(
            hwnd=row[0], pid=row[1], title=row[2], rect=(row[3], row[4], row[5], row[6]),
            minimized=bool(row[7]), maximized=bool(row[8]), topmost=bool(row[9]), dpi=row[10],
            last_checked_at=row[11], last_full_refresh_at=row[12],
        )

    def save(self, snapshot: WindowSnapshot) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO window_cache("
                " hwnd, pid, title, left, top, right, bottom, minimized, maximized, topmost, dpi,"
                " last_checked_at, last_full_refresh_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(hwnd) DO UPDATE SET"
                " pid = excluded.pid, title = excluded.title,"
                " left = excluded.left, top = excluded.top, right = excluded.right, bottom = excluded.bottom,"
                " minimized = excluded.minimized, maximized = excluded.maximized, topmost = excluded.topmost,"
                " dpi = excluded.dpi, last_checked_at = excluded.last_checked_at,"
                " last_full_refresh_at = excluded.last_full_refresh_at",
                (
                    snapshot.hwnd, snapshot.pid, snapshot.title, *snapshot.rect,
                    int(snapshot.minimized), int(snapshot.maximized), int(snapshot.topmost),
                    snapshot.dpi, snapshot.last_checked_at, snapshot.last_full_refresh_at,
                ),
            )

    def touch(self, hwnd: int, checked_at: float | None = None) -> None:
        """只更新最近检查时间，不重写完整窗口状态。"""
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE window_cache SET last_checked_at = ? WHERE hwnd = ?",
                (time.time() if checked_at is None else checked_at, hwnd),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection