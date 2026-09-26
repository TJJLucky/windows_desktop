"""服务日志初始化：应用结构化日志 + 控制台输出捕获。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO


_MAX_LOG_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5
_LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class TeeStream:
    """同时写原始控制台和日志文件，并保留调用方的文本流接口。"""

    def __init__(self, raw: TextIO, logger: logging.Logger, level: int) -> None:
        self._raw = raw
        self._logger = logger
        self._level = level
        self._buffer = ""

    @property
    def encoding(self) -> str:
        return getattr(self._raw, "encoding", "utf-8") or "utf-8"

    def isatty(self) -> bool:
        return bool(getattr(self._raw, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._raw.fileno()

    def write(self, data: str) -> int:
        if not isinstance(data, str):
            data = str(data)
        self._raw.write(data)
        self._raw.flush()
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._logger.log(self._level, "%s", line.rstrip("\r"))
        return len(data)

    def flush(self) -> None:
        self._raw.flush()
        if self._buffer.strip():
            self._logger.log(self._level, "%s", self._buffer.rstrip("\r"))
        self._buffer = ""


_configured = False


def _build_file_handler(path: Path) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=_MAX_LOG_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    return handler


def setup_logging(log_dir: Path) -> tuple[Path, Path]:
    """初始化日志，返回 (应用日志, 控制台日志) 路径。"""
    global _configured
    log_dir = log_dir.resolve()
    app_log = log_dir / "qq-desktop-service.log"
    console_log = log_dir / "qq-desktop-service.console.log"
    if _configured:
        return app_log, console_log

    original_stdout = sys.stdout
    original_stderr = sys.stderr

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(_build_file_handler(app_log))
    console_handler = logging.StreamHandler(original_stderr)
    console_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(console_handler)

    console_logger = logging.getLogger("qq_service.console")
    console_logger.setLevel(logging.INFO)
    console_logger.propagate = False
    console_logger.handlers.clear()
    console_logger.addHandler(_build_file_handler(console_log))

    sys.stdout = TeeStream(original_stdout, console_logger, logging.INFO)
    sys.stderr = TeeStream(original_stderr, console_logger, logging.INFO)
    _configured = True
    return app_log, console_log


def log_environment() -> None:
    """记录服务启动环境，便于排查 exe 和本机差异。"""
    import os
    import platform
    import sys as _sys

    logging.getLogger("qq_service.startup").info(
        "environment python=%s executable=%s platform=%s pid=%s cwd=%s",
        _sys.version.split()[0],
        _sys.executable,
        platform.platform(),
        os.getpid(),
        os.getcwd(),
    )