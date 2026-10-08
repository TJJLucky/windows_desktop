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
# ctypes：调用 Windows named mutex，阻止同一状态目录并发初始化
import ctypes
# asyncio：驱动 uvicorn 的异步事件循环
import asyncio
# logging：记录服务启动、接口执行和异常明细
import logging
# datetime / timezone：生成 endpoint 文件的 issuedAt（UTC 时间戳）
from datetime import datetime, timezone
# json：endpoint 文件的序列化 / 反序列化
import json
# hashlib：把规范化 state_dir 映射为稳定且合法的 mutex 名称
import hashlib
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
from .logging_setup import log_environment, setup_logging


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
    # 日志目录：应用结构化日志和控制台捕获日志都放在这里
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="日志目录（缺省：state-dir\\logs）",
    )
    # 解析命令行参数；argparse 会在参数非法时自动打印用法并退出
    arguments = parser.parse_args()

    # 默认模式判定：endpoint 文件与状态目录两个参数都未指定 → 走"双击即用"路径
    default_mode = arguments.endpoint_file is None and arguments.state_dir is None
    default_runtime_dir = _default_runtime_dir()
    if arguments.endpoint_file is not None:
        endpoint_file = arguments.endpoint_file.expanduser().resolve()
    elif arguments.state_dir is not None:
        endpoint_file = (arguments.state_dir.expanduser().resolve().parent / "endpoint.json").resolve()
    else:
        endpoint_file = (default_runtime_dir / "endpoint.json").resolve()

    if arguments.state_dir is not None:
        state_dir = arguments.state_dir.expanduser().resolve()
    elif arguments.endpoint_file is not None:
        state_dir = (endpoint_file.parent / "state").resolve()
    else:
        state_dir = (default_runtime_dir / "state").resolve()

    # named mutex 必须先于日志、SQLite、端口等共享资源初始化。即使两个进程在同一瞬间
    # 启动，同一个 state_dir 也只有一个进程能继续执行。
    instance_mutex = _StateDirectoryMutex.acquire(state_dir)
    if instance_mutex is None:
        existing = _existing_running(endpoint_file)
        pid_text = f" (pid={existing})" if existing is not None else ""
        print(f"[INFO] 相同状态目录的服务已在运行{pid_text}，本次退出。")
        return

    try:
        # 让窗口状态缓存跟随本次服务状态目录；显式环境变量优先。
        os.environ.setdefault("QQ_WINDOW_CACHE_DB", str((state_dir / "qq-window-cache.sqlite3").resolve()))
        log_dir = (arguments.log_dir or (state_dir / "logs")).expanduser().resolve()
        app_log, console_log = setup_logging(log_dir)
        log_environment()
        logger = logging.getLogger("qq_service.startup")
        logger.info(
            "service.config mode=%s endpoint_file=%s state_dir=%s log_dir=%s app_log=%s console_log=%s",
            "default" if default_mode else "explicit",
            endpoint_file,
            state_dir,
            log_dir,
            app_log,
            console_log,
        )
        _run_service(
            default_mode=default_mode,
            default_runtime_dir=default_runtime_dir,
            endpoint_file=endpoint_file,
            state_dir=state_dir,
            log_dir=log_dir,
            app_log=app_log,
            console_log=console_log,
            logger=logger,
        )
    finally:
        instance_mutex.close()


def _run_service(
    *,
    default_mode: bool,
    default_runtime_dir: Path,
    endpoint_file: Path,
    state_dir: Path,
    log_dir: Path,
    app_log: Path,
    console_log: Path,
    logger: logging.Logger,
) -> None:
    """在已持有 state_dir mutex 的前提下初始化并运行服务。"""
    # 绑定 loopback 监听套接字（127.0.0.1 + 系统分配随机端口），只接受本机连接
    listener = _bind_loopback_listener()
    endpoint_published = False
    try:
        # 从套接字取回操作系统实际分配的端口号
        port = int(listener.getsockname()[1])
        # 把「端口 + pid + 日志目录」原子写入 endpoint 文件（先写临时文件再 os.replace 覆盖）
        _publish_endpoint(endpoint_file, port, log_dir, app_log, console_log)
        endpoint_published = True
        logger.info("service.listening port=%d endpoint=http://127.0.0.1:%d", port, port)

        if default_mode:
            print(f"[INFO] 默认运行模式，数据目录: {default_runtime_dir}")
        print(f"[INFO] endpoint 文件: {endpoint_file}")
        print(f"[INFO] 日志目录: {log_dir}")
        print(f"[INFO] 应用日志: {app_log}")
        print(f"[INFO] 控制台日志: {console_log}")
        print(f"[INFO] 调试页面(Swagger): http://127.0.0.1:{port}/docs")

        # create_app 会初始化 SQLite，因此也必须处于 mutex 与清理保护范围内。
        app = create_app(ledger_path=state_dir / "qq-command-ledger.sqlite3")
        asyncio.run(uvicorn.Server(uvicorn.Config(app, log_level="info", use_colors=False)).serve(sockets=[listener]))
    except Exception:
        logger.exception("service.crashed")
        raise
    finally:
        logger.info("service.stopping")
        listener.close()
        if endpoint_published:
            _delete_owned_endpoint(endpoint_file)



class _StateDirectoryMutex:
    """同一 Windows 登录会话中按 state_dir 唯一的进程互斥锁。"""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, handle: int, close_handle) -> None:
        self._handle = handle
        self._close_handle = close_handle

    @staticmethod
    def name_for(state_dir: Path) -> str:
        canonical = os.path.normcase(str(state_dir.expanduser().resolve()))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        return rf"Local\price-agent-qq-service-{digest}"

    @classmethod
    def acquire(cls, state_dir: Path) -> _StateDirectoryMutex | None:
        if os.name != "nt":
            raise RuntimeError("QQ Desktop Service 仅支持 Windows")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        create_mutex.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_bool

        handle = create_mutex(None, False, cls.name_for(state_dir))
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == cls.ERROR_ALREADY_EXISTS:
            close_handle(handle)
            return None
        return cls(handle, close_handle)

    def close(self) -> None:
        if self._handle:
            self._close_handle(self._handle)
            self._handle = 0

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


def _publish_endpoint(path: Path, port: int, log_dir: Path, app_log: Path, console_log: Path) -> None:
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
            "logDirectory": str(log_dir),
            "applicationLog": str(app_log),
            "consoleLog": str(console_log),
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


# 模块被直接执行（源码调试 python -m service / 发行版 exe 入口）时调用 main()；
# 被其他模块 import 时（如 entry.py）不会触发，避免副作用
if __name__ == "__main__":
    main()
