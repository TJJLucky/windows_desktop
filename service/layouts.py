"""布局管理 HTTP 接口。数据与 GUI 实现分离，便于独立验证存储契约。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from utils.vision.layout_store import LayoutError, LayoutStore


class CreateLayout(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    sourceId: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    qqVersion: str = Field(min_length=1, max_length=80)
    theme: str = Field(min_length=1, max_length=80)
    architecture: Literal["x64", "x86", "arm64"] = "x64"
    description: str = Field(default="", max_length=1000)
    replacements: dict[str, str] = Field(default_factory=dict, max_length=12)
    recognizeAfterSave: bool = True


class UpdateLayout(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)


class LayoutDesktop:
    def __init__(self, store):
        # 延迟加载 Windows 图形依赖，API 合约测试可以注入替身。
        self.store = store

    def runtime(self):
        from utils.qq import template_runtime
        template_runtime.configure(self.store)
        return template_runtime

    @contextmanager
    def exclusive(self):
        runtime = self.runtime()
        if not runtime.operation_lock.acquire(blocking=False):
            raise LayoutError("LAYOUT_DESKTOP_BUSY", 409)
        try:
            from utils.qq.dispatcher import Dispatcher
            if Dispatcher().pending_task_count:
                raise LayoutError("LAYOUT_DESKTOP_BUSY", 409)
            yield
        finally:
            runtime.operation_lock.release()

    def recognize(self, selected):
        with self.exclusive():
            result = self.runtime().recognize(selected)
            self.store.save_recognition(selected, result)
            return result

    def activate(self, selected):
        with self.exclusive():
            from utils.qq.dispatcher import Dispatcher
            from utils.qq.region_cache import clear_region_cache
            dispatcher = Dispatcher()
            self.store.activate(selected)
            clear_region_cache()
            dispatcher.userList.clear_user_list_cache()
            dispatcher.userList.userList_region = None
            dispatcher.inputBox.input_region = None
            dispatcher.messageList.message_screen_region = None

    def desktop(self):
        return self.runtime().desktop_layout()

    def cached(self, selected):
        return self.runtime().cached_recognition(self.store, selected)


def layout_router(store: LayoutStore, desktop=None):
    desktop = desktop or LayoutDesktop(store)
    router = APIRouter(tags=["布局模板"])

    @router.get("/v1/layout-profiles")
    def profiles():
        return store.list()

    @router.post("/v1/layout-profiles", status_code=201)
    def create(request: CreateLayout):
        profile = store.create(**request.model_dump(exclude={"recognizeAfterSave"}))
        # 保存成功与识别成功独立；桌面暂不可用不回滚用户上传的模板。
        try:
            if not request.recognizeAfterSave:
                raise LayoutError("LAYOUT_DESKTOP_BUSY", 409)
            result = desktop.recognize(profile["id"])
            error = None
        except LayoutError as exc:
            result, error = None, str(exc)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("layout.create.recognition_failed")
            result, error = None, "LAYOUT_RECOGNITION_FAILED"
        return {"profile": profile, "recognition": result, "recognitionError": error}

    @router.get("/v1/layout-profiles/{profile_id}")
    def detail(profile_id: str):
        return store.detail(profile_id)

    @router.patch("/v1/layout-profiles/{profile_id}")
    def update(profile_id: str, request: UpdateLayout):
        return store.update(profile_id, **request.model_dump())

    @router.delete("/v1/layout-profiles/{profile_id}", status_code=204)
    def delete(profile_id: str):
        with desktop.exclusive():
            store.delete(profile_id)

    @router.post("/v1/layout-profiles/{profile_id}:activate")
    def activate(profile_id: str):
        desktop.activate(profile_id)
        return {"activeId": store.active_id()}

    @router.get("/v1/layout-profiles/{profile_id}/templates/{key}/image")
    def image(profile_id: str, key: str):
        return Response(store.image(profile_id, key), media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.get("/v1/desktop-layout")
    def get_desktop():
        return desktop.desktop()

    @router.get("/v1/layout-profiles/{profile_id}/recognition")
    def cached(profile_id: str):
        return desktop.cached(profile_id)

    @router.post("/v1/layout-profiles/{profile_id}:recognize")
    def recognize(profile_id: str):
        store.detail(profile_id)
        return desktop.recognize(profile_id)

    return router
