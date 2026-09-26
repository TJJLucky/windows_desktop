#!/usr/bin/env python
"""用 PyInstaller 将 price-agent-windows-desktop 打包为单文件 exe。

用法:
  python build_exe.py --project-dir E:\\Project\\agent\\windows_desktop
  python build_exe.py --project-dir <dir> --python D:\\...\\python.exe --name qq-desktop-service
  python build_exe.py --project-dir <dir> --skip-build   # 只校验现有 exe
  python build_exe.py --project-dir <dir> --smoke-start  # 构建后临时启动并请求 /v1/health

校验项:
  1. exe 存在且体积合理（<400MB；onefile 含 onnxruntime+opencv）
  2. --help 冒烟：输出含 "endpoint-file" 与 "state-dir"
  3. --smoke-start 时：临时启动 exe，读取 endpoint 文件并请求 /v1/health == READY
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SIZE_LIMIT_MB = 400

# OCR 模型与配置在 rapidocr_onnxruntime 包内，必须收集，否则 exe 运行时 OCR 找不到模型
COLLECT_DATA = ["rapidocr_onnxruntime"]

# 代码按相对路径加载（__file__ 上溯）→ onefile 解压根目录 _MEIPASS
ADD_DATA = [("templates", "templates"), ("libs/wgc_capture.dll", "libs")]

HIDDEN_IMPORTS = ["pyautogui", "pyscreeze", "mouseinfo", "pywinauto", "psutil", "pygetwindow"]


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    sys.exit(1)


def build_exe(project_dir: Path, python: str, name: str) -> Path:
    check = subprocess.run([python, "-m", "PyInstaller", "--version"], capture_output=True, text=True)
    if check.returncode != 0:
        fail("PyInstaller 未安装，请先: python -m pip install pyinstaller")

    entry = project_dir / "entry.py"
    if not entry.exists():
        fail(f"PyInstaller 入口不存在（需先创建 entry.py 包外入口）: {entry}")

    for d in ("dist-exe", "build-exe"):
        shutil.rmtree(project_dir / d, ignore_errors=True)

    cmd = [python, "-m", "PyInstaller", "--noconfirm", "--onefile", "--console",
           "--name", name,
           "--distpath", str(project_dir / "dist-exe"),
           "--workpath", str(project_dir / "build-exe"),
           "--specpath", str(project_dir / "build-exe")]
    for c in COLLECT_DATA:
        cmd += ["--collect-data", c]
    sep = ";" if os.name == "nt" else ":"
    for src, dst in ADD_DATA:
        # --add-data 相对路径按 spec 目录解析，必须用绝对路径
        cmd += ["--add-data", f"{str(project_dir / src)}{sep}{dst}"]
    for h in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", h]
    cmd.append(str(entry))

    result = subprocess.run(cmd, cwd=str(project_dir), capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"PyInstaller 失败:\n{result.stderr[-3000:]}")

    exe = project_dir / "dist-exe" / f"{name}.exe"
    if not exe.exists():
        fail(f"未生成 exe: {exe}")
    return exe


def verify_exe(exe: Path, smoke_start: bool) -> None:
    mb = exe.stat().st_size / 1048576
    print(f"  exe: {exe.name} | {mb:.1f} MB")
    if mb > SIZE_LIMIT_MB:
        fail(f"体积 {mb:.1f}MB 超上限 {SIZE_LIMIT_MB}MB")

    try:
        help_run = subprocess.run([str(exe), "--help"], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        fail("exe --help 超时（180s），疑似打包后启动异常")
    out = help_run.stdout + help_run.stderr
    for key in ("endpoint-file", "state-dir"):
        if key not in out:
            fail(f"--help 输出缺少参数: {key}")
    print("  --help 冒烟通过（endpoint-file / state-dir 均在）")

    if smoke_start:
        _smoke_start(exe)
        _smoke_default(exe)


def _smoke_start(exe: Path) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="qq-exe-smoke-"))
    ep, state = tmp / "endpoint.json", tmp / "state"
    # 必须丢弃 stdout/stderr：onefile exe 常驻会持有输出管道，父进程等 EOF 会永久卡住
    proc = subprocess.Popen(
        [str(exe), "--endpoint-file", str(ep), "--state-dir", str(state)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            if ep.exists():
                try:
                    info = json.loads(ep.read_text(encoding="utf-8"))
                    # 服务无 token 鉴权（仅监听 loopback），直接请求 /v1/health
                    req = urllib.request.Request(info["endpoint"] + "/v1/health")
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        body = json.loads(resp.read().decode("utf-8"))
                    if body.get("status") == "READY":
                        print(f"  启动冒烟通过: /v1/health -> READY (pid={info['processId']})")
                        return
                except Exception:
                    pass
            time.sleep(1)
        fail("启动冒烟超时：90s 内未健康响应")
    finally:
        # PyInstaller onefile 为父子进程模型（bootloader 派生 app 子进程），
        # terminate 只杀 bootloader；用 taskkill /T 连树清理避免残留
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _smoke_default(exe: Path) -> None:
    """默认模式冒烟：无参数双击即用（数据目录 = %LOCALAPPDATA%\\price-agent-qq-service）。

    用临时 LOCALAPPDATA 覆盖真实目录，验证：无参数启动 → endpoint 落盘 → health READY。
    """
    tmp = Path(tempfile.mkdtemp(prefix="qq-exe-default-"))
    env = {**os.environ, "LOCALAPPDATA": str(tmp)}
    ep = tmp / "price-agent-qq-service" / "endpoint.json"
    proc = subprocess.Popen(
        [str(exe)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            if ep.exists():
                try:
                    info = json.loads(ep.read_text(encoding="utf-8"))
                    # 服务无 token 鉴权（仅监听 loopback），直接请求 /v1/health
                    req = urllib.request.Request(info["endpoint"] + "/v1/health")
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        body = json.loads(resp.read().decode("utf-8"))
                    if body.get("status") == "READY":
                        print(f"  默认模式冒烟通过: 无参数启动 -> endpoint 落盘 -> /v1/health READY")
                        return
                except Exception:
                    pass
            time.sleep(1)
        fail("默认模式冒烟超时：90s 内未健康响应")
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--python", default=r"D:\anaconda3\envs\qq-desktop-service\python.exe")
    parser.add_argument("--name", default="qq-desktop-service")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--smoke-start", action="store_true")
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    if not (project_dir / "service").exists():
        fail(f"目录不是本服务项目（缺少 service/）: {project_dir}")

    if args.skip_build:
        exe = project_dir / "dist-exe" / f"{args.name}.exe"
        if not exe.exists():
            fail(f"--skip-build 但 {exe} 不存在")
    else:
        print(f"== 构建 exe（{args.python}）")
        exe = build_exe(project_dir, args.python, args.name)

    print("== 校验 exe")
    verify_exe(exe, args.smoke_start)
    print(f"\n== PASS: exe 可交付 -> {exe}")


if __name__ == "__main__":
    main()
