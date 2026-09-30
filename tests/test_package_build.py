"""PyInstaller 发布资源清单的回归测试。"""

import importlib.util
from pathlib import Path


def _load_build_script():
    project_dir = Path(__file__).resolve().parents[1]
    script_path = project_dir / ".skills" / "qq-desktop-package" / "scripts" / "build_exe.py"
    spec = importlib.util.spec_from_file_location("qq_desktop_build_exe", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pyinstaller_add_data_contains_all_native_runtime_dlls():
    """onefile EXE 必须带上 WGC 与 QQPilot InputEvent 两个 DLL。"""
    build_exe = _load_build_script()

    assert ("libs/wgc_capture.dll", "libs") in build_exe.ADD_DATA
    assert ("libs/input_event.dll", "libs") in build_exe.ADD_DATA
