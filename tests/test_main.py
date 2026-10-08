"""入口默认模式（无参数双击即用）的单元测试：默认运行目录计算 + 已在运行检测。"""

# json：构造假的 endpoint 文件内容（测试用）
import json
# os：获取当前进程 pid，构造"正在运行"的假 endpoint
import os
# Path：临时目录与路径断言
from pathlib import Path

# pytest：测试框架（tmp_path / monkeypatch fixture）
import pytest

# 被测函数：
# _default_runtime_dir —— 计算默认数据目录（%LOCALAPPDATA% 或主目录）
# _existing_running —— 依据 endpoint 文件判断是否已有实例在跑
from service.__main__ import _StateDirectoryMutex, _default_runtime_dir, _existing_running


def test_default_runtime_dir_uses_localappdata(monkeypatch):
    # 场景：Windows 环境变量 LOCALAPPDATA 存在 → 目录应落在 <LOCALAPPDATA>\price-agent-qq-service
    monkeypatch.setenv("LOCALAPPDATA", r"C:\tmp\appdata")
    # 断言：返回路径 = LOCALAPPDATA + 固定子目录名
    assert _default_runtime_dir() == Path(r"C:\tmp\appdata") / "price-agent-qq-service"


def test_default_runtime_dir_falls_back_to_home(monkeypatch):
    # 场景：LOCALAPPDATA 未设置（异常环境/非 Windows）→ 应回退到用户主目录
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    # 断言：返回路径 = 主目录 + 固定子目录名
    assert _default_runtime_dir() == Path.home() / "price-agent-qq-service"


def test_existing_running_detects_live_process(tmp_path):
    # 场景：endpoint 文件存在，且记录的 pid 是"当前存活进程"（本测试进程）→ 应识别为已在运行
    ep = tmp_path / "endpoint.json"
    # 写一个假 endpoint：processId 用当前进程 pid（必然存活）；不含 token（服务不做鉴权）
    ep.write_text(json.dumps({"processId": os.getpid()}), encoding="utf-8")
    # 断言：返回该 pid（表示检测到运行中的实例）
    assert _existing_running(ep) == os.getpid()


def test_existing_running_none_when_dead(tmp_path):
    # 场景：endpoint 文件存在，但记录的 pid 是"不可能存活的进程"（最大 32 位整数）→ 应判定无实例
    ep = tmp_path / "endpoint.json"
    # 写假 endpoint：processId = 2147483647（现实中不存在如此大的 Windows PID）；不含 token
    ep.write_text(json.dumps({"processId": 2_147_483_647}), encoding="utf-8")
    # 断言：返回 None（没有在跑的实例，允许启动）
    assert _existing_running(ep) is None


def test_existing_running_none_when_missing(tmp_path):
    # 场景：endpoint 文件根本不存在（首次运行）→ 应返回 None
    assert _existing_running(tmp_path / "absent.json") is None


def test_state_directory_mutex_name_is_stable_and_path_specific(tmp_path):
    first = _StateDirectoryMutex.name_for(tmp_path / "state-a")
    same = _StateDirectoryMutex.name_for(tmp_path / "state-a" / ".." / "state-a")
    other = _StateDirectoryMutex.name_for(tmp_path / "state-b")

    assert first == same
    assert first.startswith(r"Local\price-agent-qq-service-")
    assert first != other


def test_state_directory_mutex_close_is_idempotent():
    closed: list[int] = []
    mutex = _StateDirectoryMutex(123, lambda handle: closed.append(handle) or True)

    mutex.close()
    mutex.close()

    assert closed == [123]


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex only")
def test_state_directory_mutex_rejects_second_owner_and_releases(tmp_path):
    first = _StateDirectoryMutex.acquire(tmp_path / "state")
    assert first is not None
    try:
        assert _StateDirectoryMutex.acquire(tmp_path / "state") is None
    finally:
        first.close()

    reacquired = _StateDirectoryMutex.acquire(tmp_path / "state")
    assert reacquired is not None
    reacquired.close()
