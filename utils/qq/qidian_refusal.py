"""Read explicit qidian refusals from the local browser, on the QQ UIA owner thread."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("qq_service.qidian")


@dataclass(frozen=True)
class RefusalPage:
    hwnd: int
    runtime_id: tuple[int, ...]
    url: str


def matches_target_page(url: str, target: str) -> bool:
    """The caller's website QQ remains authoritative; the URL only binds evidence."""
    try:
        parts = urlsplit(url)
        route = urlsplit(parts.fragment)
        return (parts.scheme == "https" and parts.netloc == "admin.qidian.qq.com"
                and parts.path == "/static_proxy/b2b-qq/wpa-link/index.html"
                and not parts.query and route.path == "/person"
                and not route.scheme and not route.netloc and not route.fragment
                and parse_qs(route.query, keep_blank_values=True).get("uin") == [target])
    except ValueError:
        return False


def qidian_target_pages(target: str) -> set[RefusalPage]:
    """Return visible qidian handoff documents for the target.

    A matching document is handoff evidence only. It does not identify the
    current QQ conversation and is never used as a mapping source.
    """
    # Reuse the same thread guard as QQ; never create/cache UIA COM objects elsewhere.
    from .accessibility import _uia_client
    _uia_client()
    from pywinauto import Desktop

    pages: set[RefusalPage] = set()
    for window in Desktop(backend="uia").windows(class_name="Chrome_WidgetWin_1", visible_only=True):
        for document in window.descendants(control_type="Document"):
            # A provider error invalidates this scan, especially the pre-wake baseline.
            url = document.iface_value.CurrentValue
            if not matches_target_page(url, target):
                continue
            runtime_id = tuple(document.element_info.runtime_id)
            if not runtime_id:
                raise RuntimeError("Qidian document has no runtime identity")
            pages.add(RefusalPage(window.handle, runtime_id, url))
    return pages


def refusal_pages(target: str) -> set[RefusalPage]:
    """Return only explicit refusal pages for the target."""
    refused: set[RefusalPage] = set()
    from pywinauto import Desktop

    for window in Desktop(backend="uia").windows(class_name="Chrome_WidgetWin_1", visible_only=True):
        for document in window.descendants(control_type="Document"):
            url = document.iface_value.CurrentValue
            if not matches_target_page(url, target):
                continue
            texts = [node.window_text().replace(" ", "").strip()
                     for node in document.descendants(control_type="Text")]
            if (any("无法发起临时会话" in text for text in texts)
                    and any("您可以先添加对方为好友，再发送消息" in text for text in texts)):
                runtime_id = tuple(document.element_info.runtime_id)
                if not runtime_id:
                    raise RuntimeError("Qidian document has no runtime identity")
                refused.add(RefusalPage(window.handle, runtime_id, url))
    return refused


@dataclass(frozen=True)
class AddFriendProfile:
    hwnd: int
    runtime_id: tuple[int, ...]
    target: str


def add_friend_profiles(target: str) -> set[AddFriendProfile]:
    """Only a visible QQ-owned profile with this exact number and add-only action.

    This is an exclusion policy, not a claim that every stranger cannot chat.
    Never inspect colors, click Add Friend, or treat an unrelated card as evidence.
    """
    import psutil
    import win32gui
    import win32process
    from .accessibility import _iuia, UIA
    uia = _iuia()
    profiles: set[AddFriendProfile] = set()

    def inspect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindowText(hwnd) != "资料卡":
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if psutil.Process(pid).name().lower() != "qq.exe":
            return
        root = uia.ElementFromHandle(hwnd)
        nodes = root.FindAll(UIA.TreeScope_Descendants, uia.CreateTrueCondition())
        texts, buttons, profile = [], set(), None
        for index in range(nodes.Length):
            node = nodes.GetElement(index)
            if node.CurrentIsOffscreen:
                continue
            if "buddy-profile" in (node.CurrentClassName or "").split():
                profile = node
            name = (node.CurrentName or "").strip()
            if node.CurrentControlType == UIA.UIA_TextControlTypeId:
                texts.append(name)
            elif node.CurrentControlType == UIA.UIA_ButtonControlTypeId:
                buttons.add(name)
        # Bind the number to the QQ label, not any number in address/signature.
        number_matches = any(value == "QQ" and texts[i + 1:i + 2] == [target]
                             for i, value in enumerate(texts))
        if (profile is not None and number_matches and buttons & {"加好友", "添加好友"}
                and not buttons & {"发消息", "发送消息", "聊天"}):
            runtime_id = tuple(profile.GetRuntimeId())
            if not runtime_id:
                raise RuntimeError("QQ profile has no runtime identity")
            profiles.add(AddFriendProfile(hwnd, runtime_id, target))

    win32gui.EnumWindows(inspect, None)
    return profiles


class QidianRefusalProbe:
    """Read explicit refusal; matching add-only profiles are exclusion, not identity."""
    def __init__(self, target: str):
        self.target = target
        self.next_scan = 0.0
        self.previous_profiles: set[AddFriendProfile] = set()
        self.baseline: set[RefusalPage] | None = None
        try:
            self.baseline = refusal_pages(target)
        except Exception as exc:
            logger.info("qidian.refusal.baseline_unavailable target=%s reason=%s", target, exc)

    def poll(self) -> RefusalPage | AddFriendProfile | None:
        # QQ reuses existing profile windows. Require two fresh observations of
        # this exact number + add-only controls, not a new HWND/runtime ID. This
        # exclusion never authorizes mapping or sending to the current chat.
        try:
            profiles = add_friend_profiles(self.target)
            confirmed = profiles & self.previous_profiles
            self.previous_profiles = profiles
            if confirmed:
                profile = next(iter(confirmed))
                logger.warning("qidian.refusal.profile_confirmed target=%s hwnd=%s action=add_friend_only evidence=two_fresh_reads",
                               self.target, profile.hwnd)
                return profile
        except Exception as exc:
            self.previous_profiles = set()
            logger.info("qidian.profile.scan_unavailable target=%s type=%s", self.target, type(exc).__name__)

        now = time.monotonic()
        if self.baseline is None or now < self.next_scan:
            return None
        try:
            fresh = refusal_pages(self.target) - self.baseline
            if fresh:
                page = next(iter(fresh))
                logger.warning("qidian.refusal.confirmed target=%s hwnd=%s url=%s elapsed_ms=%.1f",
                               self.target, page.hwnd, page.url, (time.monotonic() - now) * 1000)
                return page
        except Exception as exc:
            logger.info("qidian.refusal.scan_unavailable target=%s reason=%s", self.target, exc)
        finally:
            self.next_scan = time.monotonic() + 0.75
        return None
