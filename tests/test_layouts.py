"""版本持久化、HTTP 边界及真实截图上的版本隔离；不点击/发送 QQ。"""
import base64
import io
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from PIL import Image

from service.app import create_app
from utils.vision.layout_store import DEFAULT_ID, TEMPLATES, LayoutError, LayoutStore


def new_profile(store, **overrides):
    return store.create(**dict(sourceId=DEFAULT_ID, name="自定义", qqVersion="9.9.35-52804", theme="测试", architecture="x64", **overrides))


def png(image):
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_copy_number_template_persists_and_upgrade_does_not_overwrite(tmp_path):
    path = tmp_path / "layouts.db"
    store = LayoutStore(path)
    data = png(Image.new("RGB", (40, 20), "blue"))
    custom = new_profile(store, replacements={"copy_qq_number": base64.b64encode(data).decode()})
    with store.connect() as db:
        db.execute("PRAGMA user_version=1")
    reopened = LayoutStore(path)
    assert reopened.image(custom["id"], "copy_qq_number") == data
    with reopened.connect() as db:
        db.execute("DELETE FROM images WHERE profile_id=? AND key='copy_qq_number'", (custom["id"],))
        db.execute("PRAGMA user_version=1")
    upgraded = LayoutStore(path)
    assert upgraded.image(custom["id"], "copy_qq_number") == upgraded.image(DEFAULT_ID, "copy_qq_number")
    # 完成升级后缺图是错误，不靠重启自动修补。
    with upgraded.connect() as db:
        db.execute("DELETE FROM images WHERE profile_id=? AND key='copy_qq_number'", (custom["id"],))
    with pytest.raises(LayoutError, match="LAYOUT_TEMPLATE_MISSING"):
        LayoutStore(path).image(custom["id"], "copy_qq_number")


def test_persistence_complete_copy_default_and_active_protection(tmp_path):
    store = LayoutStore(tmp_path / "layouts.db")
    original = store.image(DEFAULT_ID, "search_bar_left")
    replacement = png(Image.fromarray(np.random.default_rng(9).integers(0, 255, (25, 40, 3), dtype=np.uint8)))
    custom = new_profile(store, replacements={"search_bar_left": base64.b64encode(replacement).decode()})
    assert len(custom["templates"]) == len(TEMPLATES)
    assert store.image(DEFAULT_ID, "search_bar_left") == original
    assert store.image(custom["id"], "search_bar_left") == replacement
    assert store.image(custom["id"], "copy_icon") == store.image(DEFAULT_ID, "copy_icon")
    store.activate(custom["id"])
    store.save_recognition(custom["id"], {"capturedAt": time.time(), "templates": []})
    restarted = LayoutStore(store.path)
    assert restarted.active_id() == custom["id"]
    assert restarted.image(custom["id"], "search_bar_left") == replacement
    assert restarted.recognition(custom["id"])["bootId"] != restarted.boot_id
    for operation in (lambda: store.delete(DEFAULT_ID), lambda: store.update(DEFAULT_ID, "overwrite", ""), lambda: store.delete(custom["id"])):
        with pytest.raises(LayoutError) as error:
            operation()
        assert error.value.status == 409
    store.update(custom["id"], "改名", "说明")
    assert store.detail(custom["id"])["name"] == "改名"
    store.activate(DEFAULT_ID)
    store.delete(custom["id"])
    assert len(store.list()["profiles"]) == 1


@pytest.mark.parametrize("data", [b"not a png", png(Image.new("RGBA", (12, 12), (0, 0, 0, 0))), png(Image.new("RGB", (4097, 1)))])
def test_invalid_upload_rolls_back_entire_version(tmp_path, data):
    store = LayoutStore(tmp_path / "layouts.db")
    with pytest.raises(LayoutError, match="LAYOUT_IMAGE_INVALID"):
        new_profile(store, replacements={"copy_icon": base64.b64encode(data).decode()})
    assert len(store.list()["profiles"]) == 1


def test_missing_active_image_does_not_silently_use_default(tmp_path):
    store = LayoutStore(tmp_path / "layouts.db")
    custom = new_profile(store)
    with store.connect() as db:
        db.execute("DELETE FROM images WHERE profile_id=? AND key='copy_icon'", (custom["id"],))
    with pytest.raises(LayoutError, match="LAYOUT_TEMPLATE_MISSING"):
        store.activate(custom["id"])
    assert store.active_id() == DEFAULT_ID


class BusyDesktop:
    @contextmanager
    def exclusive(self):
        yield

    def recognize(self, selected):
        raise LayoutError("LAYOUT_DESKTOP_BUSY", 409)


@pytest.mark.asyncio
async def test_http_saved_even_when_recognition_busy_and_strict_metadata(tmp_path):
    app = create_app(automation=object(), ledger_path=tmp_path / "ledger.db", layout_desktop=BusyDesktop())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request = dict(sourceId=DEFAULT_ID, name="HTTP 自定义", qqVersion="9.9", theme="白", architecture="x64", replacements={})
        response = await client.post("/v1/layout-profiles", json=request)
        assert response.status_code == 201
        result = response.json()
        assert result["recognitionError"] == "LAYOUT_DESKTOP_BUSY"
        selected = result["profile"]["id"]
        assert (await client.get(f"/v1/layout-profiles/{selected}")).status_code == 200
        image = await client.get(f"/v1/layout-profiles/{selected}/templates/copy_icon/image")
        assert image.headers["content-type"] == "image/png" and image.content.startswith(b"\x89PNG")
        assert (await client.patch(f"/v1/layout-profiles/{selected}", json={"name": "rename", "description": "", "replacements": {}})).status_code == 422
        assert (await client.delete(f"/v1/layout-profiles/{DEFAULT_ID}")).status_code == 409
        assert (await client.post("/v1/layout-profiles", json={**request, "path": "C:/secret"})).status_code == 422
        assert (await client.get(f"/v1/layout-profiles/{selected}/templates/secret/image")).status_code == 404


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from utils.qq import template_runtime as rt, window_ops
    from utils.qq.region_cache import clear_region_cache
    store = LayoutStore(tmp_path / "layouts.db")
    monkeypatch.setattr(rt, "_store", store)
    image = Image.open(Path(__file__).parents[1] / "debug" / "655d3acd056c0ef6e31363698cda717e.png").convert("RGB")
    window = SimpleNamespace(_hWnd=123, left=-1200, top=35, right=-1200 + image.width, bottom=35 + image.height, isMinimized=False, isMaximized=False)
    desktop = dict(left=-1920, top=0, right=1920, bottom=1080, monitors=[dict(left=-1920, top=0, right=0, bottom=1080, dpi=96, primary=False), dict(left=0, top=0, right=1920, bottom=1080, dpi=96, primary=True)])
    monkeypatch.setattr(rt, "desktop_layout", lambda: desktop)
    monkeypatch.setattr(rt, "window_signature", lambda win: dict(hwnd=win._hWnd, pid=88, left=win.left, top=win.top, right=win.right, bottom=win.bottom, dpi=96))
    monkeypatch.setattr(window_ops, "_main_window_candidates", lambda: [window])
    monkeypatch.setattr(window_ops, "ensure_qq_window_with_retry", lambda: window)
    monkeypatch.setattr(window_ops, "get_qq_window_image", lambda win: (image, win))
    clear_region_cache()
    yield rt, store, window, image
    clear_region_cache()


def test_real_fixture_recognition_positions_and_changed_template(runtime):
    rt, store, window, image = runtime
    first = rt.recognize(DEFAULT_ID)
    assert first["regions"]["userlist"]["imageRect"]["left"] == 60
    found = next(t for t in first["templates"] if t["key"] == "inputbox_top_left")
    assert found["status"] == "matched"
    assert found["screenRect"]["left"] == found["imageRect"]["left"] - 1200
    replacement = png(Image.fromarray(np.random.default_rng(77).integers(0, 255, (70, 90, 3), dtype=np.uint8)))
    custom = new_profile(store, replacements={"inputbox_top_left": base64.b64encode(replacement).decode()})
    second = rt.recognize(custom["id"])
    assert next(t for t in second["templates"] if t["key"] == "inputbox_top_left")["status"] == "not_found"
    assert second["regions"]["inputbox"]["status"] == "not_found"
    assert store.active_id() == DEFAULT_ID  # 预览不启用。
    assert rt.cached_recognition(store, DEFAULT_ID)["valid"]
    window.left += 5
    assert rt.cached_recognition(store, DEFAULT_ID)["staleReason"] == "desktop_changed"
    assert rt.cached_recognition(LayoutStore(store.path), DEFAULT_ID)["staleReason"] == "service_restarted"


def test_profile_is_fixed_for_operation_and_cache_isolated(runtime):
    rt, store, window, image = runtime
    from utils.qq.region_cache import get_region, save_region
    custom = new_profile(store)
    with rt.operation_scope(DEFAULT_ID):
        save_region("test", window, image, {"region": {"left": 1}})
        store.activate(custom["id"])
        assert rt.profile_id() == DEFAULT_ID
    with rt.operation_scope():
        assert rt.profile_id() == custom["id"]
        assert get_region("test", window, image) is None


def test_fixed_regions_reuse_matches_after_30_seconds_but_manual_recognition_is_fresh(runtime, monkeypatch):
    rt, _, window, image = runtime
    from utils.qq import region_cache, regions
    now = region_cache.time.monotonic()
    monkeypatch.setattr(region_cache.time, "monotonic", lambda: now)
    first = rt.recognize(DEFAULT_ID)
    calls = []
    original = rt._find_template
    def match(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(rt, "_find_template", match)
    monkeypatch.setattr(region_cache.time, "monotonic", lambda: now + 3600)
    updated = image.copy()
    updated.paste("black", (80, image.height - 30, 150, image.height - 10))
    with rt.operation_scope(DEFAULT_ID):
        for name, fn in (("userlist", regions.get_userList_region_and_image),
                         ("inputbox", regions.get_inputbox_region_and_image),
                         ("messagebox", regions.get_message_box_region_and_image)):
            result = fn(window, updated)
            assert result.image_region == first["regions"][name]["imageRect"]
            assert result.full_image is updated
        assert not calls
    assert result.image.tobytes() == updated.crop(tuple(result.image_region[k] for k in
                                                       ("left", "top", "right", "bottom"))).tobytes()
    rt.recognize(DEFAULT_ID)
    assert calls  # 手动检测仍按当前图像重新匹配，不用长缓存掩盖结果。


def test_recognition_expiry_keeps_positions_and_ignores_retired_templates(runtime, monkeypatch):
    rt, store, _, _ = runtime
    previous = rt.recognize(DEFAULT_ID)
    previous["templates"].append(dict(key="input_buttom_active", status="matched"))
    store.save_recognition(DEFAULT_ID, previous)
    monkeypatch.setattr(rt.time, "time", lambda: previous["capturedAt"] + 29.9)
    assert rt.cached_recognition(store, DEFAULT_ID)["valid"]
    monkeypatch.setattr(rt.time, "time", lambda: previous["capturedAt"] + 30)
    expired = rt.cached_recognition(store, DEFAULT_ID)
    assert expired["staleReason"] == "expired"
    assert expired["regions"] == previous["regions"]
    assert all(not item["key"].startswith("input_buttom") for item in expired["templates"])
    assert all(not item["key"].startswith("input_buttom") for item in store.detail(DEFAULT_ID)["templates"])


def test_desktop_management_does_not_interrupt_operation(runtime):
    rt, store, _, _ = runtime
    from service.layouts import LayoutDesktop
    ready, release = threading.Event(), threading.Event()
    def hold():
        with rt.operation_scope():
            ready.set()
            release.wait(5)
    worker = threading.Thread(target=hold)
    worker.start()
    assert ready.wait(2)
    try:
        with pytest.raises(LayoutError, match="LAYOUT_DESKTOP_BUSY"):
            LayoutDesktop(store).activate(DEFAULT_ID)
    finally:
        release.set()
        worker.join(3)


def test_recognition_prepares_window_before_capture_and_uses_new_geometry(runtime, monkeypatch):
    rt, store, window, image = runtime
    from utils.qq import window_ops
    events = []
    def prepare():
        events.append("capture_check")
        window.left, window.top = 0, 0
        window.right, window.bottom = image.width, image.height
        return window
    def capture(win):
        assert events == ["capture_check"]
        events.append("capture")
        return image, win
    monkeypatch.setattr(window_ops, "ensure_qq_window_with_retry", prepare)
    monkeypatch.setattr(window_ops, "get_qq_window_image", capture)
    result = rt.recognize(DEFAULT_ID)
    assert events == ["capture_check", "capture"]
    assert result["window"]["left"] == 0
    matched = next(t for t in result["templates"] if t["status"] == "matched")
    assert matched["screenRect"] == matched["imageRect"]


def test_failed_capture_check_never_uses_old_window(runtime, monkeypatch):
    rt, store, _, _ = runtime
    previous = rt.recognize(DEFAULT_ID)
    assert rt.cached_recognition(store, DEFAULT_ID)["valid"]
    from utils.qq import window_ops
    def fail():
        raise window_ops.QQWindowNotReadyError("not ready")
    monkeypatch.setattr(window_ops, "ensure_qq_window_with_retry", fail)
    monkeypatch.setattr(window_ops, "get_qq_window_image", lambda _: pytest.fail("must not capture after readiness failure"))
    with pytest.raises(LayoutError, match="QQ_WINDOW_NOT_READY"):
        rt.recognize(DEFAULT_ID)
    cached = rt.cached_recognition(store, DEFAULT_ID)
    assert not cached["valid"]
    assert cached["staleReason"] == "recognition_failed"
    assert any(item["status"] == "matched" for item in cached["templates"])
    assert cached["window"] == previous["window"]
    assert cached["templates"] == previous["templates"]
    assert cached["capturedAt"] == previous["capturedAt"]
    restarted = rt.cached_recognition(LayoutStore(store.path), DEFAULT_ID)
    assert not restarted["valid"]
    assert restarted["templates"] == previous["templates"]
