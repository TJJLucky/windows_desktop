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


def refusal_pages(target: str) -> set[RefusalPage]:
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
            texts = [node.window_text().replace(" ", "").strip()
                     for node in document.descendants(control_type="Text")]
            if (any("无法发起临时会话" in text for text in texts)
                    and any("您可以先添加对方为好友，再发送消息" in text for text in texts)):
                runtime_id = tuple(document.element_info.runtime_id)
                if not runtime_id:
                    raise RuntimeError("Qidian document has no runtime identity")
                pages.add(RefusalPage(window.handle, runtime_id, url))
    return pages


class QidianRefusalProbe:
    """Only a newly observed refusal after this wake may exclude a conversation."""
    def __init__(self, target: str):
        self.target = target
        self.next_scan = 0.0
        self.baseline: set[RefusalPage] | None = None
        try:
            self.baseline = refusal_pages(target)
        except Exception as exc:
            logger.info("qidian.refusal.baseline_unavailable target=%s reason=%s", target, exc)

    def poll(self) -> RefusalPage | None:
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
