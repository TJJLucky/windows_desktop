"""外部接口层合同测试；不操作真实 QQ。

用 FakeAutomation 替身替换真实视觉 RPA，验证：
- 无鉴权可访问（服务不做 token 校验，任何请求头/无请求头均可调用）；
- v1 合同映射（health / contacts / read / capture 的结构正确）；
- 发送幂等（同 commandId 只执行一次，可查询）；
- 幂等键冲突（同 ID 不同消息体 → 409）；
- 截图可用性探测（就绪 200+ready=true；不可用仍 200+ready=false）。
"""

# 延迟求值类型注解
from __future__ import annotations

# httpx：异步 HTTP 客户端（ASGI transport 直连应用，不起真实服务器）
import httpx
# pytest：测试框架
import pytest

# create_app：被测应用工厂
from service.app import create_app
from service.facade import QqContactNotFoundError


class FakeAutomation:
    """自动化替身：不操作 QQ，记录发送调用并返回固定数据。"""

    def __init__(self) -> None:
        # 记录所有发送调用（用于断言幂等：同消息应只发送一次）
        self.sent: list[tuple[str, str]] = []
        # 截图可用性开关（测试可翻转为 False 模拟窗口不可用）
        self.capture_ready: bool = True

    def list_contacts(self) -> dict[str, dict]:
        # 固定返回一个联系人
        return {"华强电子": {"name": "华强电子", "active": True, "new_msg": False}}

    def send_message(self, contact_name: str, text: str) -> dict:
        # 记录调用并返回成功结果
        self.sent.append((contact_name, text))
        return {"ok": True, "sent": True, "textLength": len(text), "error": None}

    def read_messages(self, contact_name: str) -> dict:
        # 固定返回一条 QQ 原生复制解析消息
        return {
            "ok": True,
            "messages": [
                {
                    "sender": "TJJ",
                    "timestamp": "09-30 12:00:01",
                    "text": "有货",
                    "rawText": "TJJ: 09-30 12:00:01 有货",
                }
            ],
            "count": 1,
            "copiedText": "TJJ: 09-30 12:00:01 有货",
            "error": None,
        }

    def check_capture_ready(self) -> dict:
        # 按开关返回就绪/不可用两种结果
        if not self.capture_ready:
            return {"ready": False, "error": "QQ_WINDOW_NOT_READY"}
        return {"ready": True, "windowTitle": "QQ"}


@pytest.fixture
def service(tmp_path):
    """构建 (替身, 应用) 元组：每个测试独立临时账本库。"""
    # 创建替身
    automation = FakeAutomation()
    # 用替身构造应用（无 token 参数——服务不做鉴权）
    return automation, create_app(automation, ledger_path=tmp_path / "ledger.sqlite3")


def client(app):
    """构造异步测试客户端（不携带任何鉴权头，验证无 token 可访问）。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://qq-service.test",
    )


@pytest.mark.asyncio
async def test_api_works_without_token(service) -> None:
    """场景：不带任何 Authorization 头请求 → 直接可用（项目不做 token 鉴权）。"""
    _, app = service
    # 不带鉴权头直接请求健康检查
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qq-service.test") as caller:
        response = await caller.get("/v1/health")
    # 断言：200（无需 token）
    assert response.status_code == 200
    # 断言：健康状态固定值
    assert response.json() == {"apiVersion": "v1", "status": "READY"}


@pytest.mark.asyncio
async def test_api_projects_legacy_automation_as_v1_contract(service) -> None:
    """场景：直接调用三个接口（无鉴权）→ 结构符合 v1 合同。"""
    _, app = service
    async with client(app) as caller:
        # 健康检查
        health = await caller.get("/v1/health")
        # 联系人列表
        contacts = await caller.post("/v1/contacts:query")
        # 读取消息
        messages = await caller.post("/v1/commands/read", json={"contactName": "华强电子"})
    # 断言：健康状态固定值
    assert health.json() == {"apiVersion": "v1", "status": "READY"}
    # 断言：1 个联系人
    assert contacts.json()["count"] == 1
    # 断言：读到的消息文本正确
    assert messages.json()["messages"][0]["text"] == "有货"
    assert messages.json()["messages"][0]["sender"] == "TJJ"
    assert "direction" not in messages.json()["messages"][0]


@pytest.mark.asyncio
async def test_read_returns_clear_not_found_message(service) -> None:
    """联系人不在当前可见列表时，读取接口直接返回一次明确的 404。"""
    automation, app = service

    def fail_read(contact_name: str) -> dict:
        raise QqContactNotFoundError(contact_name)

    automation.read_messages = fail_read
    async with client(app) as caller:
        response = await caller.post("/v1/commands/read", json={"contactName": "不存在"})

    assert response.status_code == 404
    assert response.json() == {
        "detail": "QQ_CONTACT_NOT_FOUND: 当前可见 QQ 用户列表中未找到联系人「不存在」"
    }


@pytest.mark.asyncio
async def test_send_is_idempotent_and_queryable(service) -> None:
    """场景：相同 commandId 发送两次 → 只执行一次，可查询且结果一致。"""
    automation, app = service
    # 请求体（commandId 固定）
    payload = {"commandId": "effect-key-0000001", "contactName": "华强电子", "text": "请报价"}
    async with client(app) as caller:
        # 第一次发送（应真正执行）
        first = await caller.post("/v1/commands/send", json=payload)
        # 第二次同 ID 同消息（应幂等返回，不重发）
        duplicate = await caller.post("/v1/commands/send", json=payload)
        # 按 ID 查询状态
        result = await caller.get("/v1/commands/effect-key-0000001")
    # 断言：第一次成功
    assert first.json()["status"] == "SUCCEEDED"
    # 断言：幂等重复结果 == 首次结果 == 查询结果
    assert duplicate.json() == first.json() == result.json()
    # 断言：替身只收到一次发送（幂等生效）
    assert automation.sent == [("华强电子", "请报价")]


@pytest.mark.asyncio
async def test_same_command_id_cannot_change_message(service) -> None:
    """场景：同 commandId 配不同消息体 → 409 冲突。"""
    _, app = service
    async with client(app) as caller:
        # 第一次发送
        await caller.post(
            "/v1/commands/send",
            json={"commandId": "effect-key-0000002", "contactName": "华强电子", "text": "第一次"},
        )
        # 同 ID 改消息体
        conflict = await caller.post(
            "/v1/commands/send",
            json={"commandId": "effect-key-0000002", "contactName": "华强电子", "text": "第二次"},
        )
    # 断言：409 + 冲突错误码
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "QQ_COMMAND_ID_REUSED"}


@pytest.mark.asyncio
async def test_capture_check_ready(service) -> None:
    """场景：窗口就绪 → 截图可用，返回 ready=true + 窗口标题。"""
    _, app = service
    async with client(app) as caller:
        # 调用截图可用性检查
        response = await caller.post("/v1/capture:check")
    # 断言：200（探测接口）
    assert response.status_code == 200
    body = response.json()
    # 就绪 + 检测方法固定 + 窗口标题 + 无错误
    assert body["ready"] is True
    assert body["method"] == "ensure_qq_window_with_retry"
    assert body["windowTitle"] == "QQ"
    assert body["error"] is None


@pytest.mark.asyncio
async def test_capture_check_not_ready_is_200(service) -> None:
    """场景：窗口不可用 → 仍 200 + ready=false（探测语义，不 503）。"""
    automation, app = service
    # 翻转替身开关：模拟 QQ 窗口未就绪
    automation.capture_ready = False
    async with client(app) as caller:
        # 调用截图可用性检查
        response = await caller.post("/v1/capture:check")
    # 断言：不可用也是有效检测结果，200 而非 503
    assert response.status_code == 200
    body = response.json()
    # 未就绪 + 无窗口标题 + 错误码带出
    assert body["ready"] is False
    assert body["windowTitle"] is None
    assert body["error"] == "QQ_WINDOW_NOT_READY"
