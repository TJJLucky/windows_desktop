"""独立启动 QQ Desktop Service。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="PriceAgent QQ Desktop Service")
    parser.add_argument(
        "--endpoint-file",
        type=Path,
        default=None,
        help="endpoint 文件路径（缺省：%%LOCALAPPDATA%%\\price-agent-qq-service\\endpoint.json）",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="状态目录，存放聊天记录/账本数据库（缺省：同 endpoint 文件目录下 state\\）",
    )
    parser.add_argument("--token")
    arguments = parser.parse_args()
    token = arguments.token or secrets.token_urlsafe(32)
    default_mode = arguments.endpoint_file is None and arguments.state_dir is None
    runtime_dir = _default_runtime_dir() if default_mode else None
    endpoint_file = (arguments.endpoint_file or (runtime_dir / "endpoint.json")).resolve()
    state_dir = (arguments.state_dir or (runtime_dir / "state")).resolve()
    if default_mode:
        existing = _existing_running(endpoint_file)
        if existing is not None:
            print(f"[INFO] 服务已在运行 (pid={existing})，直接使用现有实例，本次退出。")
            return
    listener = _bind_loopback_listener()
    port = int(listener.getsockname()[1])
    _publish_endpoint(endpoint_file, port, token)
    if default_mode:
        print(f"[INFO] 默认运行模式，数据目录: {runtime_dir}")
    print(f"[INFO] endpoint 文件: {endpoint_file}")
    print(f"[INFO] 调试页面(Swagger): http://127.0.0.1:{port}/docs")
    app = create_app(token=token, ledger_path=state_dir / "qq-command-ledger.sqlite3")
    try:
        asyncio.run(uvicorn.Server(uvicorn.Config(app, log_level="info")).serve(sockets=[listener]))
    finally:
        listener.close()
        _delete_owned_endpoint(endpoint_file, token)


def _default_runtime_dir() -> Path:
    """默认数据目录：Windows 用 %LOCALAPPDATA%\\price-agent-qq-service，其余平台用 ~/.price-agent-qq-service。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    else:
        base = str(Path.home())
    return Path(base) / "price-agent-qq-service"


def _existing_running(endpoint_file: Path) -> int | None:
    """默认模式下若已有实例在跑（endpoint 文件存在且进程存活），返回其 pid，否则 None。"""
    try:
        payload = json.loads(endpoint_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    pid = payload.get("processId")
    if not isinstance(pid, int):
        return None
    try:
        import psutil
        return pid if psutil.pid_exists(pid) else None
    except Exception:
        return None


def _bind_loopback_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise


def _publish_endpoint(path: Path, port: int, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "schemaVersion": 1,
            "service": "price-agent-qq-service",
            "apiVersion": "v1",
            "processId": os.getpid(),
            "endpoint": f"http://127.0.0.1:{port}",
            "token": token,
            "issuedAt": datetime.now(timezone.utc).isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _delete_owned_endpoint(path: Path, token: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return
    if payload.get("processId") == os.getpid() and payload.get("token") == token:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
