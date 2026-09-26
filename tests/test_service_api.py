"""外部接口层合同测试；不操作真实 QQ。"""

from __future__ import annotations

import httpx
import pytest

from service.app import create_app


class FakeAutomation:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def list_contacts(self) -> dict[str, dict]:
        return {"华强电子": {"name": "华强电子", "active": True, "new_msg": False}}

    def send_message(self, contact_name: str, text: str) -> dict:
        self.sent.append((contact_name, text))
        return {"ok": True, "sent": True, "textLength": len(text), "error": None}

    def read_messages(self, contact_name: str) -> dict:
        return {
            "ok": True,
            "messages": [{"text": "有货", "isSelf": False, "x": 1, "y": 2, "w": 3, "h": 4}],
            "count": 1,
            "error": None,
        }


@pytest.fixture
def service(tmp_path):
    automation = FakeAutomation()
    return automation, create_app(automation, token="test-token", ledger_path=tmp_path / "ledger.sqlite3")


def client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://qq-service.test",
        headers={"Authorization": "Bearer test-token"},
    )


@pytest.mark.asyncio
async def test_api_requires_token(service) -> None:
    _, app = service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qq-service.test") as caller:
        response = await caller.get("/v1/health")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_api_projects_legacy_automation_as_v1_contract(service) -> None:
    _, app = service
    async with client(app) as caller:
        health = await caller.get("/v1/health")
        contacts = await caller.post("/v1/contacts:query")
        messages = await caller.post("/v1/commands/read", json={"contactName": "华强电子"})
    assert health.json() == {"apiVersion": "v1", "status": "READY"}
    assert contacts.json()["count"] == 1
    assert messages.json()["messages"][0]["text"] == "有货"


@pytest.mark.asyncio
async def test_send_is_idempotent_and_queryable(service) -> None:
    automation, app = service
    payload = {"commandId": "effect-key-0000001", "contactName": "华强电子", "text": "请报价"}
    async with client(app) as caller:
        first = await caller.post("/v1/commands/send", json=payload)
        duplicate = await caller.post("/v1/commands/send", json=payload)
        result = await caller.get("/v1/commands/effect-key-0000001")
    assert first.json()["status"] == "SUCCEEDED"
    assert duplicate.json() == first.json() == result.json()
    assert automation.sent == [("华强电子", "请报价")]


@pytest.mark.asyncio
async def test_same_command_id_cannot_change_message(service) -> None:
    _, app = service
    async with client(app) as caller:
        await caller.post(
            "/v1/commands/send",
            json={"commandId": "effect-key-0000002", "contactName": "华强电子", "text": "第一次"},
        )
        conflict = await caller.post(
            "/v1/commands/send",
            json={"commandId": "effect-key-0000002", "contactName": "华强电子", "text": "第二次"},
        )
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "QQ_COMMAND_ID_REUSED"}
