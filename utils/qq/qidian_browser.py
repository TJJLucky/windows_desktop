"""Run qidian wakes through one dedicated, serialized browser slot and close it after use."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4
import winreg

import psutil
import win32api
import win32con
import win32gui
import win32process

logger = logging.getLogger("qq_service.qidian")

_WINDOW_CLASS = "Chrome_WidgetWin_1"
_BROWSER_NAMES = {"msedge.exe", "chrome.exe"}


def _profile_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "price-agent-qq-service" / "qidian-browser-profile"


def _browser_executable() -> str | None:
    """Return a Chromium browser that can be owned and closed deterministically.

    The Windows default handler is preferred only when it is Edge/Chrome. A
    non-Chromium default (or a missing association) must not fall back to
    ``os.startfile`` because that produces an unmanaged window.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as key:
            progid = winreg.QueryValueEx(key, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\shell\open\command") as key:
            command = winreg.QueryValueEx(key, None)[0]
        executable = win32api.CommandLineToArgv(command)[0]
        if Path(executable).name.lower() in _BROWSER_NAMES and Path(executable).is_file():
            return executable
    except (OSError, IndexError):
        logger.info("qidian.browser.default_unmanaged")

    candidates = []
    for env_name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        root = Path(base)
        candidates.extend([
            root / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            root / "Google" / "Chrome" / "Application" / "chrome.exe",
        ])
    for name in ("msedge.exe", "chrome.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _all_windows() -> set[int]:
    handles: set[int] = set()

    def collect(hwnd, _):
        if win32gui.IsWindow(hwnd) and win32gui.GetClassName(hwnd) == _WINDOW_CLASS:
            handles.add(hwnd)

    win32gui.EnumWindows(collect, None)
    return handles


def _same_profile_arg(argument: str, profile: Path) -> bool:
    prefix = "--user-data-dir="
    if not argument.lower().startswith(prefix):
        return False
    value = argument[len(prefix):].strip().strip('"')
    if not value:
        return False
    try:
        return os.path.normcase(os.path.abspath(value)) == os.path.normcase(os.path.abspath(profile))
    except (OSError, ValueError):
        return False


class QidianBrowserWindow:
    """One browser window at a time, owned by a dedicated Edge/Chrome profile."""

    def __init__(self, link: str, target: str):
        self.link = link
        self.target = target
        self.owner = "price-agent-wake-" + uuid4().hex
        self.profile = _profile_dir()
        self.executable: str | None = None
        self.process: subprocess.Popen | None = None
        self.browser_pids: set[int] = set()
        self.owned_windows: set[tuple[int, int]] = set()

    def open(self, *, wait_for_window: bool = True) -> None:
        self.executable = _browser_executable()
        if not self.executable:
            raise OSError("未找到可管理的 Edge/Chrome，无法保证 qidian 窗口可关闭")

        self.profile.mkdir(parents=True, exist_ok=True)
        # A crashed/previous wake must never leave a second qidian window behind.
        self._close_managed(reason="pre_open", announce=False)
        command = [
            self.executable,
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--new-window",
            self.link,
        ]
        self.process = subprocess.Popen(
            command,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.browser_pids.add(self.process.pid)
        logger.info("qidian.browser.opened owner=%s pid=%s profile=%s", self.owner, self.process.pid, self.profile)
        if wait_for_window:
            self._capture_owned_window(time.monotonic() + 5.0)
            logger.info(
                "qidian.browser.slot_ready owner=%s windows=%s",
                self.owner,
                ",".join(str(hwnd) for hwnd, _ in sorted(self.owned_windows)) or "pending",
            )
        else:
            logger.info("qidian.browser.slot_ready owner=%s windows=async", self.owner)

    def _capture_owned_window(self, deadline: float) -> None:
        next_refresh = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_refresh:
                self._refresh_profile_processes()
                next_refresh = now + 0.25
            windows = self._managed_windows()
            if windows:
                self.owned_windows.update(windows)
                return
            if self.process is not None and self.process.poll() is not None and not self._managed_processes():
                return
            time.sleep(0.1)

    def _refresh_profile_processes(self) -> None:
        """Refresh the dedicated browser process tree once per short capture interval."""
        matched: dict[int, psutil.Process] = {}
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info.get("name") or "").lower() not in _BROWSER_NAMES:
                    continue
                if any(_same_profile_arg(str(arg), self.profile) for arg in proc.cmdline()):
                    matched[proc.pid] = proc
            except (psutil.Error, OSError):
                continue
        if self.process is not None:
            try:
                matched[self.process.pid] = psutil.Process(self.process.pid)
            except (psutil.Error, OSError):
                pass
        for proc in list(matched.values()):
            try:
                for child in proc.children(recursive=True):
                    matched[child.pid] = child
            except (psutil.Error, OSError):
                pass
        self.browser_pids.update(matched)

    def _managed_processes(self) -> list[psutil.Process]:
        managed: dict[int, psutil.Process] = {}
        for pid in self.browser_pids:
            try:
                proc = psutil.Process(pid)
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    managed[pid] = proc
            except (psutil.Error, OSError):
                continue
        return list(managed.values())

    def _managed_windows(self) -> set[tuple[int, int]]:
        process_ids = {proc.pid for proc in self._managed_processes()}
        if not process_ids:
            return set()
        windows: set[tuple[int, int]] = set()
        for hwnd in _all_windows() | {item[0] for item in self.owned_windows}:
            try:
                if not win32gui.IsWindow(hwnd):
                    continue
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid in process_ids:
                    windows.add((hwnd, pid))
            except (OSError, win32gui.error):
                continue
        return windows

    def _terminate_managed_processes(self) -> None:
        processes: dict[int, psutil.Process] = {proc.pid: proc for proc in self._managed_processes()}
        for proc in list(processes.values()):
            try:
                for child in proc.children(recursive=True):
                    processes[child.pid] = child
            except (psutil.Error, OSError):
                pass
        if not processes:
            return
        ordered = sorted(processes.values(), key=lambda proc: proc.pid, reverse=True)
        for proc in ordered:
            try:
                proc.terminate()
            except (psutil.Error, OSError):
                pass
        _, alive = psutil.wait_procs(ordered, timeout=1.5)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.Error, OSError):
                pass
        psutil.wait_procs(alive, timeout=1.0)

    def _close_managed(self, *, reason: str, announce: bool = True) -> None:
        self._refresh_profile_processes()
        windows = self._managed_windows()
        for hwnd, _ in windows:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except (OSError, win32gui.error):
                pass
        if windows and announce:
            logger.info(
                "qidian.browser.close_requested owner=%s reason=%s windows=%s",
                self.owner,
                reason,
                ",".join(str(hwnd) for hwnd, _ in sorted(windows)),
            )

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not self._managed_windows() and not self._managed_processes():
                break
            time.sleep(0.1)

        self._terminate_managed_processes()
        remaining_windows = self._managed_windows()
        remaining_processes = self._managed_processes()
        if not remaining_windows and not remaining_processes:
            if announce:
                logger.info("qidian.browser.close_confirmed owner=%s reason=%s", self.owner, reason)
            return
        logger.warning(
            "qidian.browser.cleanup_unconfirmed owner=%s reason=%s windows=%s processes=%s",
            self.owner,
            reason,
            ",".join(str(hwnd) for hwnd, _ in sorted(remaining_windows)) or "none",
            ",".join(str(proc.pid) for proc in remaining_processes) or "none",
        )

    def close(self) -> None:
        if not self.executable:
            return
        self._close_managed(reason="wake_finished")
