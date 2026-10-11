"""固定一次操作的模板版本，复用 QQ 就绪流程，收集同帧实际匹配坐标。"""
from __future__ import annotations

import copy
import io
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from PIL import Image

from ..vision.layout_store import DEFAULT_ID, TEMPLATES, LayoutError, LayoutStore
from ..vision.matcher import find_template as _find_template

operation_lock = threading.RLock()
_store: LayoutStore | None = None
_session = ContextVar("qq_template_session", default=None)


def configure(store: LayoutStore):
    global _store
    _store = store


def profile_id():
    session = _session.get()
    return session["profileId"] if session else (_store.active_id() if _store else DEFAULT_ID)


def load_image(filename):
    key = filename.removesuffix(".png")
    if key not in TEMPLATES:
        raise LayoutError("LAYOUT_TEMPLATE_NOT_FOUND", 404)
    session = _session.get()
    if session is not None and key in session["images"]:
        return session["images"][key]
    if _store:
        image = Image.open(io.BytesIO(_store.image(profile_id(), key)))
    else:
        image = Image.open(Path(__file__).resolve().parents[2] / "templates" / filename)
    image.load()
    image.info["templateKey"] = key
    if session is not None:
        session["images"][key] = image
    return image


def desktop_layout():
    import ctypes
    import win32api
    monitors = []
    for handle, _, rect in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(handle)
        dx, dy = ctypes.c_uint(96), ctypes.c_uint(96)
        try:
            ctypes.windll.shcore.GetDpiForMonitor(int(handle), 0, ctypes.byref(dx), ctypes.byref(dy))
        except (OSError, AttributeError):
            pass
        monitors.append(dict(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3],
                             dpi=dx.value, primary=bool(info["Flags"] & 1)))
    if not monitors:
        raise LayoutError("LAYOUT_DESKTOP_UNAVAILABLE", 503)
    return dict(left=min(m["left"] for m in monitors), top=min(m["top"] for m in monitors),
                right=max(m["right"] for m in monitors), bottom=max(m["bottom"] for m in monitors), monitors=monitors)


def window_signature(window):
    import ctypes
    import win32process
    _, pid = win32process.GetWindowThreadProcessId(window._hWnd)
    return dict(hwnd=int(window._hWnd), pid=pid, left=int(window.left), top=int(window.top),
                right=int(window.right), bottom=int(window.bottom), dpi=int(ctypes.windll.user32.GetDpiForWindow(window._hWnd)))


def record_frame(window, image):
    session = _session.get()
    if session is None or session.get("frame") is image:
        return
    session.update(frame=image, window=window_signature(window), desktop=desktop_layout(),
                   templates={}, regions={}, capturedAt=time.time())


def observing():
    session = _session.get()
    return bool(session and session.get("force"))


def template_observations():
    session = _session.get()
    return copy.deepcopy(session.get("templates", {})) if session else {}


def restore_observations(items):
    session = _session.get()
    if session is not None:
        session["templates"].update(copy.deepcopy(items))


def find_template(big, small, threshold=0.8, use_mask=False, multiscale=True, *, origin=(0, 0)):
    result = _find_template(big, small, threshold, use_mask, multiscale)
    session = _session.get()
    key = small.info.get("templateKey")
    if session is not None and key and session.get("window"):
        previous = session["templates"].get(key)
        if result is not None or previous is None or previous["status"] != "matched":
            item = dict(key=key, status="matched" if result else "not_found")
            if result:
                rect = {k: int(result[k]) + origin[0 if k in ("left", "right") else 1] for k in ("left", "top", "right", "bottom")}
                item.update(imageRect=rect, screenRect=to_screen(rect, session["window"]))
            session["templates"][key] = item
    return result


def to_screen(rect, window):
    return {k: rect[k] + window["left" if k in ("left", "right") else "top"] for k in ("left", "top", "right", "bottom")}


def record_region(name, result):
    session = _session.get()
    if session is None:
        return
    matched = {k for k, v in session["templates"].items() if v["status"] == "matched"}
    fallback = ((name == "userlist" and "search_bar_left" not in matched) or
                (name in ("inputbox", "messagebox") and not matched.intersection({"inputbox_top_right1", "inputbox_top_right2"})))
    session["regions"][name] = dict(status="fallback" if fallback else "matched", screenRect=result.screen_region, imageRect=result.image_region)


def snapshot(session):
    if not session.get("window"):
        return None
    return dict(profileId=session["profileId"], snapshotId=uuid.uuid4().hex, capturedAt=session["capturedAt"],
                window=session["window"], desktop=session["desktop"], valid=True, staleReason=None,
                templates=[session["templates"].get(key, dict(key=key, status="not_checked")) for key in TEMPLATES],
                regions=session["regions"])


@contextmanager
def operation_scope(selected=None):
    # Dispatcher 与管理操作共用此锁，版本不会在一次点击/读取/发送中途改变。
    with operation_lock:
        session = dict(profileId=selected or (_store.active_id() if _store else DEFAULT_ID), images={})
        token = _session.set(session)
        completed = False
        try:
            yield session
            completed = True
        finally:
            result = snapshot(session)
            if _store and (result or not completed):
                try:
                    if completed:
                        _store.save_recognition(session["profileId"], result)
                    else:
                        _store.mark_recognition_stale(session["profileId"], "recognition_failed")
                except Exception:
                    logging.getLogger(__name__).exception("layout.recognition.persist_failed")
            _session.reset(token)


def cached_recognition(store, selected):
    result = store.recognition(selected)
    if result is None:
        return dict(profileId=selected, valid=False, staleReason="not_recognized", templates=[], regions={})
    reason = None
    if result.pop("bootId", None) != store.boot_id:
        reason = "service_restarted"
    elif not result.get("valid", True):
        reason = result.get("staleReason") or "recognition_failed"
    elif time.time() - result["capturedAt"] >= 30:
        reason = "expired"
    else:
        try:
            from .window_ops import _main_window_candidates
            windows = _main_window_candidates()
            if len(windows) != 1 or window_signature(windows[0]) != result["window"] or desktop_layout() != result["desktop"]:
                reason = "desktop_changed"
        except Exception:
            reason = "window_unavailable"
    result["templates"] = [item for item in result.get("templates", []) if item["key"] in TEMPLATES]
    result.update(valid=reason is None, staleReason=reason)
    return result


def recognize(selected):
    from .window_ops import ensure_qq_window_with_retry, get_qq_window_image, QQWindowNotReadyError
    from .regions import (get_userList_region_and_image, get_message_box_region_and_image,
                          get_inputbox_region_and_image, get_copy_action_region)
    with operation_scope(selected) as session:
        session["force"] = True
        # 与 /v1/capture:check 共用同一就绪入口。先恢复/整理 QQ 到约定位置，
        # 再取图；整个过程持有桌面操作锁，不与联系人/发送队列交叉。
        try:
            window = ensure_qq_window_with_retry()
        except QQWindowNotReadyError as exc:
            raise LayoutError("QQ_WINDOW_NOT_READY", 409) from exc
        image, _ = get_qq_window_image(window)
        if image is None:
            raise LayoutError("QQ_WINDOW_NOT_READY", 409)
        record_frame(window, image)
        for name, fn in (("userlist", get_userList_region_and_image), ("messagebox", get_message_box_region_and_image), ("inputbox", get_inputbox_region_and_image)):
            try:
                fn(window, image)
            except RuntimeError:
                session["regions"][name] = dict(status="not_found")
        get_copy_action_region(window, image)
        for key in ("switch_to_big", "switch_to_small"):
            find_template(image.crop((0, 0, image.width, min(40, image.height))), load_image(f"{key}.png"), 1)
        if window_signature(window) != session["window"] or desktop_layout() != session["desktop"]:
            session.pop("window", None)
            raise LayoutError("LAYOUT_DESKTOP_CHANGED", 409)
        return snapshot(session)
