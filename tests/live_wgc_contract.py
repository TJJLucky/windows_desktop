"""Explicit live native validation: two disposable Win32 windows, no QQ actions.

Run from the project root with the service Conda environment after building the DLL.
This is intentionally separate from pytest's desktop-free regression suite.
"""
import ctypes as c
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

import win32api
import win32con
import win32gui

from utils.core.screenshot import WgcFrameInfo, ensure_dpi_aware


def main():
    ensure_dpi_aware()
    ready = threading.Event()
    windows = []
    failure = []
    user = c.WinDLL("user32")
    user.SetTimer.argtypes = (wintypes.HWND, c.c_size_t, wintypes.UINT, c.c_void_p)
    user.SetTimer.restype = c.c_size_t

    def ui():
        def proc(hwnd, message, wp, lp):
            if message == win32con.WM_PAINT:
                dc, paint = win32gui.BeginPaint(hwnd)
                brush = win32gui.CreateSolidBrush(win32api.RGB(70, 100, 130))
                try:
                    win32gui.FillRect(dc, win32gui.GetClientRect(hwnd), brush)
                finally:
                    win32gui.DeleteObject(brush)
                    win32gui.EndPaint(hwnd, paint)
                return 0
            if message == win32con.WM_TIMER:
                # Same pixels, new presentation. The capture engine itself never requests redraws.
                win32gui.InvalidateRect(hwnd, None, False)
                win32gui.UpdateWindow(hwnd)
                return 0
            if message == win32con.WM_APP:
                win32gui.PostQuitMessage(0)
                return 0
            return win32gui.DefWindowProc(hwnd, message, wp, lp)
        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = "WgcContractWindow"
            wc.lpfnWndProc = proc
            atom = win32gui.RegisterClass(wc)
            for x in (1600, 2000):
                hwnd = win32gui.CreateWindow(atom, "WGC contract", win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE,
                                            x, 200, 360, 260, 0, 0, wc.hInstance, None)
                windows.append(hwnd)
                user.SetTimer(hwnd, 1, 40, None)
            ready.set()
            win32gui.PumpMessages()
            for hwnd in windows:
                if win32gui.IsWindow(hwnd):
                    win32gui.DestroyWindow(hwnd)
            win32gui.UnregisterClass(wc.lpszClassName, wc.hInstance)
        except BaseException as error:
            failure.append(error)
            ready.set()

    thread = threading.Thread(target=ui)
    thread.start()
    ready.wait(10)
    if failure:
        raise failure[0]
    dll = c.CDLL(str(Path("native/wgc_capture/x64/Release/wgc_capture.dll").resolve()))
    dll.WgcOpen.argtypes = [wintypes.HWND, c.POINTER(c.c_uint64)]
    dll.WgcRead.argtypes = [c.c_uint64, c.POINTER(c.c_uint8), c.c_uint32,
                           c.c_int64, c.c_uint32, c.POINTER(WgcFrameInfo)]
    dll.WgcClose.argtypes = [c.c_uint64]
    dll.WgcClock100ns.restype = c.c_int64
    dll.WgcSnapshot.argtypes = [wintypes.HWND, c.POINTER(c.c_uint8), c.c_int,
                               c.POINTER(c.c_int), c.POINTER(c.c_int)]
    checks = []

    def opened(hwnd):
        value = c.c_uint64()
        assert dll.WgcOpen(hwnd, c.byref(value)) == 1
        return value.value

    def read(session, *, capacity=1024 * 1024, future=False):
        buffer = (c.c_uint8 * capacity)()
        info = WgcFrameInfo()
        info.struct_size = c.sizeof(info)
        barrier = dll.WgcClock100ns() + (10000000 if future else 0)
        started = time.perf_counter()
        status = dll.WgcRead(session, buffer, capacity, barrier, 300, c.byref(info))
        if status == 1:
            assert info.frame_time_100ns > barrier and info.arrival_time_100ns > barrier
        return status, info, c.string_at(buffer, min(info.required_bytes, capacity)), (time.perf_counter() - started) * 1000

    try:
        session = opened(windows[0])
        assert opened(windows[0]) == session
        checks.append("same_window_reuses_session")
        first = read(session)
        assert first[0] == 1
        second = read(session)
        assert second[0] == 1 and second[1].frame_number > first[1].frame_number
        assert first[2] == second[2]
        checks.append("equal_pixels_new_frame")
        tiny = read(session, capacity=1)
        assert tiny[0] == -4 and tiny[1].required_bytes > 1 and tiny[2] == b"\0"
        checks.append("insufficient_buffer_does_not_write")
        timeout = read(session, future=True)
        assert timeout[0] == -3 and 290 <= timeout[3] < 600
        checks.append("future_barrier_rejects_stale_until_timeout")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: read(session), range(8)))
        assert all(result[0] == 1 for result in results)
        checks.append("concurrent_native_reads")
        win32gui.SetWindowPos(windows[0], 0, 0, 0, 480, 320,
                              win32con.SWP_NOMOVE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        time.sleep(.3)
        resized = read(session)
        # The captured surface is the client area; it must grow with the window without a hard error.
        assert resized[0] == 1
        assert resized[1].width > first[1].width and resized[1].height > first[1].height
        assert resized[1].generation != first[1].generation
        checks.append("resize_rebuilds_pool_and_generation")
        win32gui.ShowWindow(windows[0], win32con.SW_MINIMIZE)
        time.sleep(.3)
        assert read(session)[0] == -2
        win32gui.ShowWindow(windows[0], win32con.SW_RESTORE)
        time.sleep(.3)
        restored = opened(windows[0])
        assert restored != session and read(restored)[0] == 1
        checks.append("minimize_restore_reopens")
        win32gui.ShowWindow(windows[0], win32con.SW_HIDE)
        time.sleep(.3)
        assert read(restored)[0] == -2
        win32gui.ShowWindow(windows[0], win32con.SW_SHOW)
        time.sleep(.3)
        restored = opened(windows[0])
        assert read(restored)[0] == 1
        checks.append("hide_show_reopens")
        win32gui.PostMessage(windows[0], win32con.WM_CLOSE, 0, 0)
        time.sleep(.3)
        assert read(restored)[0] == -2
        replacement = opened(windows[1])
        assert replacement != restored and read(replacement)[0] == 1
        assert dll.WgcClose(restored) == 1
        assert read(replacement)[0] == 1
        checks.append("destroy_replace_and_stale_close")
        buffer = (c.c_uint8 * (1024 * 1024))()
        width, height = c.c_int(), c.c_int()
        assert dll.WgcSnapshot(windows[1], buffer, len(buffer), c.byref(width), c.byref(height)) == 1
        assert opened(windows[1]) == replacement
        checks.append("legacy_snapshot_same_engine")
        assert dll.WgcClose(replacement) == dll.WgcClose(replacement) == 1
        assert read(replacement)[0] == -5
        checks.append("repeated_close_and_closed_read")
        print(json.dumps({"passed": checks}, indent=2), flush=True)
    finally:
        dll.WgcClose(0)
        if win32gui.IsWindow(windows[1]):
            win32gui.PostMessage(windows[1], win32con.WM_APP, 0, 0)
        thread.join(10)
        assert not thread.is_alive()


if __name__ == "__main__":
    main()
