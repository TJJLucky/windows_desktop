"""入口默认模式（无参数双击即用）：默认运行目录与已在运行检测。"""

import json
import os
from pathlib import Path

import pytest

from service.__main__ import _default_runtime_dir, _existing_running


def test_default_runtime_dir_uses_localappdata(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\tmp\appdata")
    assert _default_runtime_dir() == Path(r"C:\tmp\appdata") / "price-agent-qq-service"


def test_default_runtime_dir_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert _default_runtime_dir() == Path.home() / "price-agent-qq-service"


def test_existing_running_detects_live_process(tmp_path):
    ep = tmp_path / "endpoint.json"
    ep.write_text(json.dumps({"processId": os.getpid(), "token": "x"}), encoding="utf-8")
    assert _existing_running(ep) == os.getpid()


def test_existing_running_none_when_dead(tmp_path):
    ep = tmp_path / "endpoint.json"
    ep.write_text(json.dumps({"processId": 2_147_483_647, "token": "x"}), encoding="utf-8")
    assert _existing_running(ep) is None


def test_existing_running_none_when_missing(tmp_path):
    assert _existing_running(tmp_path / "absent.json") is None
