"""独立启动 QQ Desktop Service。

本文件是整个服务的进程入口，负责：
1. 解析命令行参数（默认模式：无参数双击即用；显式模式：指定 endpoint 文件与状态目录）；
2. 在 127.0.0.1 上绑定一个随机空闲端口（只服务本机，不开放公网）；
3. 把「端口 + pid」原子写入 endpoint 文件，供调用方（如 UniApp 前端）读取后调用 API；
4. 启动 uvicorn 事件循环托管 FastAPI 应用，服务退出时清理自己写出的 endpoint 文件。

注意：服务不做 token 鉴权——安全边界是"仅监听 127.0.0.1（loopback）"，
调用方不需要也无法使用 Authorization 头。
"""

# 让类型注解支持"延迟求值"：字符串形式的注解不必在 import 阶段立即解析，
# 从而允许模块内的前向引用（如函数返回自身类等），也加快启动速度。
from __future__ import annotations

# argparse：命令行参数解析（--endpoint-file / --state-dir）
import argparse
# asyncio：驱动 uvicorn 的异步事件循环
import asyncio
# datetime / timezone：生成 endpoint 文件的 issuedAt（UTC 时间戳）
from datetime import datetime, timezone
# json：endpoint 文件的序列化 / 反序列化
import json
# os：获取当前进程 pid（写入 endpoint 文件）、判断操作系统类型（Windows 数据目录选择）
import os
# Path：跨平台路径对象，统一处理 endpoint 文件与状态目录
from pathlib import Path
# socket：创建 loopback 监听套接字，绑定随机端口
import socket
# tempfile：以"先写临时文件再原子替换"的方式写 endpoint 文件，避免调用方读到半截内容
import tempfile

# uvicorn：ASGI 服务器，负责把 HTTP 请求转交给 FastAPI 应用处理
import uvicorn

# create_app：构造 FastAPI 应用（路由都在里面注册）
from .app import create_app


def main() -> None:
    """命令行入口：解析参数 → 准备运行目录 → 发布 endpoint → 启动 uvicorn 常驻服务。"""

    # 用 argparse 声明两个可选参数：
    parser = argparse.ArgumentParser(description="PriceAgent QQ Desktop Service")
    # endpoint 文件：调用方读取「端口」的位置；缺省时使用默认运行目录下的 endpoint.json
    parser.add_argument(
        "--endpoint-file",
        type=Path,
        default=None,
        help="endpoint 文件路径（缺省：%%LOCALAPPDATA%%\\price-agent-qq-service\\endpoint.json）",
    )
    # 状态目录：存放聊天记录 / 命令账本两个 SQLite 数据库；缺省时使用默认运行目录下的 state\ 子目录
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="状态目录，存放聊天记录/账本数据库（缺省：同 endpoint 文件目录下 state\\）",
    )
    # 解析命令行参数；argparse 会在参数非法时自动打印用法并退出
    arguments = parser.parse_args()

    # 默认模式判定：endpoint 文件与状态目录两个参数都未指定 → 走"双击即用"路径
    default_mode = arguments.endpoint_file is None and arguments.state_dir is None
    # 默认模式时计算默认运行目录（Windows 为 %LOCALAPPDATA%\price-agent-qq-service）
    runtime_dir = _default_runtime_dir() if default_mode else None
    # endpoint 文件最终路径：显式参数优先；否则放在默认运行目录下
    endpoint_file = (arguments.endpoint_file or (runtime_dir / "endpoint.json")).resolve()
    # 状态目录最终路径：显式参数优先；否则为默认运行目录下的 state 子目录
    state_dir = (arguments.state_dir or (runtime_dir / "state")).resolve()

    # 默认模式下做"防重复拉起"检查：若已有实例在跑，直接提示并退出，避免双击两次起两个服务
    if default_mode:
        existing = _existing_running(endpoint_file)
        if existing is not None:
            # 打印友好提示后直接 return（不再绑定端口、不发布 endpoint、不启动 uvicorn）
            print(f"[INFO] 服务已在运行 (pid={existing})，直接使用现有实例，本次退出。")
            return

    # 绑定 loopback 监听套接字（127.0.0.1 + 系统分配随机端口），只接受本机连接
    listener = _bind_loopback_listener()
    # 从套接字取回操作系统实际分配的端口号
    port = int(listener.getsockname()[1])
    # 把「端口 + pid」原子写入 endpoint 文件（先写临时文件再 os.replace 覆盖）
    _publish_endpoint(endpoint_file, port)

    # 默认模式额外打印数据目录，方便最终用户知道状态数据落在哪里
    if default_mode:
        print(f"[INFO] 默认运行模式，数据目录: {runtime_dir}")
    # 打印 endpoint 文件路径（调用方需要读取它拿端口）
    print(f"[INFO] endpoint 文件: {endpoint_file}")
    # 打印调试页地址：Swagger UI（/docs），浏览器打开即可直接发请求调试（无需 token）
    print(f"[INFO] 调试页面(Swagger): http://127.0.0.1:{port}/docs")

    # 构造 FastAPI 应用：传入账本数据库路径（聊天记录库路径由 create_app 自动推导）
    app = create_app(ledger_path=state_dir / "qq-command-ledger.sqlite3")
    try:
        # 用 asyncio 运行 uvicorn 服务：serve 绑定到已就绪的 listener 上（不再二次 bind）
        # sockets 参数传入监听套接字，保证端口就是我们上面绑定的那个随机端口
        asyncio.run(uvicorn.Server(uvicorn.Config(app, log_level="info")).serve(sockets=[listener]))
    finally:
        # 无论正常退出还是异常中断，都要先关闭监听套接字
        listener.close()
        # 再清理 endpoint 文件：只有确认该文件是本进程写出的（pid 匹配）才删除，
        # 避免误删其他实例或旧实例留下的文件
        _delete_owned_endpoint(endpoint_file)


def _default_runtime_dir() -> Path:
    """计算默认数据目录：Windows 用 %LOCALAPPDATA%\\price-agent-qq-service，其余平台用 ~/.price-agent-qq-service。"""
    # Windows 用 LOCALAPPDATA（用户级应用数据目录，无需管理员权限即可写）
    if os.name == "nt":
        # 环境变量 LOCALAPPDATA 通常存在；万一缺失则退回用户主目录，保证总有一个可写位置
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    else:
        # 非 Windows（调试/开发用）：放用户主目录下的隐藏目录
        base = str(Path.home())
    # 统一在基准目录下加子目录 price-agent-qq-service，避免污染其他应用的数据
    return Path(base) / "price-agent-qq-service"


def _existing_running(endpoint_file: Path) -> int | None:
    """默认模式下防重复启动：endpoint 文件存在且其中记录的进程仍存活，则返回其 pid，否则返回 None。"""
    try:
        # 尝试读取 endpoint 文件；文件不存在 / 无法解析（如空文件、坏 JSON）都视为"没有在跑的实例"
        payload = json.loads(endpoint_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    # 取出记录的服务进程 pid；不是合法整数则无法判断，直接视为没有实例
    pid = payload.get("processId")
    if not isinstance(pid, int):
        return None
    try:
        # 用 psutil 判断该 pid 是否还活着（pid 复用风险低：同 pid 仍活着基本就是同一实例）
        import psutil
        return pid if psutil.pid_exists(pid) else None
    except Exception:
        # psutil 异常（如初始化失败）时保守返回 None，不阻塞本次启动
        return None


def _bind_loopback_listener() -> socket.socket:
    """在 127.0.0.1 上绑定一个系统分配的随机端口，并返回非阻塞监听套接字。"""
    # 创建 TCP 流式套接字（IPv4）
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # SO_EXCLUSIVEADDRUSE 是 Windows 专用选项：独占端口地址，防止别的进程劫持同一端口
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # 绑定 loopback 地址 + 端口 0：端口 0 表示让操作系统分配一个当前空闲的随机端口
        listener.bind(("127.0.0.1", 0))
        # 进入监听状态，backlog=128 表示内核排队等待 accept 的连接数上限
        listener.listen(128)
        # 设为非阻塞：供 uvicorn 的事件循环直接接管（uvicorn 会把它注册进 epoll/select）
        listener.setblocking(False)
        return listener
    except BaseException:
        # 绑定/监听过程中任何异常都要释放套接字，避免句柄泄漏
        listener.close()
        raise


def _publish_endpoint(path: Path, port: int) -> None:
    """把「端口 + pid」原子写入 endpoint 文件：先写同目录临时文件，再 os.replace 覆盖。"""
    # 确保 endpoint 文件所在目录存在（默认模式首次运行需要创建目录）
    path.parent.mkdir(parents=True, exist_ok=True)
    # 组装要发布的元数据：schemaVersion 固定 1（契约版本）、服务名、API 版本、
    # 当前进程 pid（供清理与防重复检测）、loopback 端点、发布时间（UTC）。
    # 注意：本服务不做 token 鉴权，endpoint 文件不含 token 字段。
    payload = json.dumps(
        {
            "schemaVersion": 1,
            "service": "price-agent-qq-service",
            "apiVersion": "v1",
            "processId": os.getpid(),
            "endpoint": f"http://127.0.0.1:{port}",
            "issuedAt": datetime.now(timezone.utc).isoformat(),
        },
        ensure_ascii=False,      # 保留非 ASCII 字符原样输出（本项目字段均为 ASCII，防御性设置）
        separators=(",", ":"),   # 压缩 JSON：逗号与冒号后不加空格，减小文件体积
    )
    # 在同目录创建临时文件（隐藏文件名前缀 .、后缀 .tmp），保证与目标文件同文件系统（os.replace 才可靠）
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        # 以文本方式写入 JSON；newline="" 避免换行符被改写，fsync 确保落盘（防止断电丢数据）
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # 原子替换：把临时文件改名为目标路径。读者要么看到旧文件要么看到新文件，绝不会看到半截内容
        os.replace(temporary, path)
    finally:
        # 无论成功与否都清理临时文件（成功时它已被重命名走，unlink 静默忽略不存在）
        temporary.unlink(missing_ok=True)


def _delete_owned_endpoint(path: Path) -> None:
    """服务退出时清理 endpoint 文件：只有确认是本进程写出的才删除，防止误删他人文件。"""
    try:
        # 读回当前文件内容；不存在/损坏则无需清理
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return
    # 校验：记录的进程 pid 是本进程 → 才能删除（服务不做 token 鉴权，无需再比对 token）
    if payload.get("processId") == os.getpid():
        path.unlink(missing_ok=True)


# 模块被直接执行（python -m service / exe 入口）时调用 main()；
# 被其他模块 import 时（如 entry.py）不会触发，避免副作用
if __name__ == "__main__":
    main()
