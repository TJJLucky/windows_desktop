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
    parser.add_argument("--endpoint-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--token")
    arguments = parser.parse_args()
    token = arguments.token or secrets.token_urlsafe(32)
    listener = _bind_loopback_listener()
    endpoint_file = arguments.endpoint_file.resolve()
    _publish_endpoint(endpoint_file, int(listener.getsockname()[1]), token)
    app = create_app(token=token, ledger_path=arguments.state_dir.resolve() / "qq-command-ledger.sqlite3")
    try:
        asyncio.run(uvicorn.Server(uvicorn.Config(app, log_level="info")).serve(sockets=[listener]))
    finally:
        listener.close()
        _delete_owned_endpoint(endpoint_file, token)


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
