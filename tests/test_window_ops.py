"""QQ 主窗口冲突处理测试。"""

from types import SimpleNamespace

from utils.qq import window_ops


def test_get_main_window_resets_duplicate_titled_windows(monkeypatch):
    first = SimpleNamespace(_hWnd=101, title="QQ")
    second = SimpleNamespace(_hWnd=202, title="qq")
    recovered = SimpleNamespace(_hWnd=303, title="QQ")
    monkeypatch.setattr(window_ops, "get_qq_windows", lambda: [first, second])
    calls = []
    monkeypatch.setattr(
        window_ops, "_reset_duplicate_main_windows",
        lambda windows: calls.append([win._hWnd for win in windows]) or recovered,
    )

    assert window_ops.get_main_window() is recovered
    assert calls == [[101, 202]]


def test_get_main_window_ignores_non_main_titles(monkeypatch):
    main = SimpleNamespace(_hWnd=101, title="QQ")
    chat = SimpleNamespace(_hWnd=202, title="联系人")
    monkeypatch.setattr(window_ops, "get_qq_windows", lambda: [chat, main])

    assert window_ops.get_main_window() is main
