"""聊天记录存储与 facade 接入测试；不操作真实 QQ。

覆盖：
- ChatHistoryStore 单测（追加/去重/seq/会话列表/方向校验）；
- API 集成：send/read 成功后自动落库；
- 查询端点：查询前自动更新一次（update=ok）且去重；
- 查询端点：自动更新失败降级（update=failed）不阻断历史。
"""

# 延迟求值类型注解
from __future__ import annotations

# httpx：异步 HTTP 客户端
import httpx
# pytest：测试框架
import pytest

# create_app：被测应用工厂
from service.app import create_app
# ChatHistoryStore：聊天记录存储
from service.chat_history import ChatHistoryStore
# QqAutomationError：视觉自动化失败异常（降级场景用）
from service.facade import QqAutomationError


@pytest.fixture
def store(tmp_path):
    """每个测试独立临时聊天库。"""
    return ChatHistoryStore(tmp_path / "chat.sqlite3")


def test_append_and_dedup(store):
    """场景：同联系人同文本 60s 内重复 → 去重跳过；不同方向/联系人 → 写入。"""
    # 首次写入（返回 True 表示已落库）
    assert store.append("华强电子", "in", "有货") is True
    # 60s 内重复读取 → 去重跳过（返回 False）
    assert store.append("华强电子", "in", "有货") is False   # 60s 内重复读取 → 跳过
    # 不同方向（我方发送）→ 写入
    assert store.append("华强电子", "out", "收到", "cmd-1") is True
    # 不同联系人同文本 → 写入
    assert store.append("张三", "in", "有货") is True        # 不同联系人同文本 → 写入


def test_seq_per_contact(store):
    """场景：seq 按联系人独立递增（每人从 1 开始）。"""
    # 写入两条华强电子消息
    store.append("华强电子", "in", "a")
    store.append("华强电子", "in", "b")
    # 写入一条张三消息（独立 seq）
    store.append("张三", "in", "a")
    # 华强电子 seq 应为 [1, 2]
    assert [m["seq"] for m in store.get_history("华强电子")] == [1, 2]
    # 张三第一条 seq 为 1
    assert store.get_history("张三")[0]["seq"] == 1


def test_conversations(store):
    """场景：会话聚合（total 条数 + 最后一条文本）。"""
    # 写入一进一出
    store.append("华强电子", "in", "有货")
    store.append("华强电子", "out", "收到")
    # 列出会话
    conv = store.list_conversations()
    # 只有一个会话
    assert len(conv) == 1
    # 2 条消息 + 最后文本为"收到"
    assert conv[0]["total"] == 2 and conv[0]["lastText"] == "收到"


def test_direction_validation(store):
    """场景：非法方向值 → 抛 ValueError。"""
    # 非 in/out 的方向应拒绝
    with pytest.raises(ValueError):
        store.append("华强电子", "side", "x")


class _RecordingAutomation:
    """与 FakeAutomation 同契约：记录发送并回读消息。"""

    def __init__(self) -> None:
        # 记录发送调用
        self.sent: list[tuple[str, str]] = []

    def list_contacts(self) -> dict[str, dict]:
        # 固定一个联系人
        return {"华强电子": {"name": "华强电子", "active": True, "new_msg": False}}

    def send_message(self, contact_name: str, text: str) -> dict:
        # 记录调用并返回成功
        self.sent.append((contact_name, text))
        return {"ok": True, "sent": True, "textLength": len(text), "error": None}

    def read_messages(self, contact_name: str) -> dict:
        # 固定返回一条消息
        return {
            "ok": True,
            "messages": [{"text": "有货", "isSelf": False, "x": 1, "y": 2, "w": 3, "h": 4}],
            "count": 1,
            "error": None,
        }

    def check_capture_ready(self) -> dict:
        # 窗口就绪（与 FakeAutomation 同契约）
        return {"ready": True, "windowTitle": "QQ"}


class _ReadFailAutomation(_RecordingAutomation):
    """视觉读取失败的替身：自动更新降级场景。"""

    def read_messages(self, contact_name: str) -> dict:
        # 读取抛异常（模拟 QQ 视觉读取失败）
        raise QqAutomationError("QQ_MESSAGES_UNAVAILABLE")


def test_create_app_writes_chat_history(tmp_path):
    """API 层集成：read/send 成功后 store 落库（不触真实 QQ）。"""
    # 替身 + 临时库路径
    automation = _RecordingAutomation()
    ledger_path = tmp_path / "ledger.sqlite3"
    chat_path = tmp_path / "chat.sqlite3"
    # 应用工厂（注入聊天库路径）
    app = create_app(automation, token="test-token", ledger_path=ledger_path, chat_history_path=chat_path)

    async def run():
        # 带 token 调用 send 与 read
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://qq-service.test",
            headers={"Authorization": "Bearer test-token"},
        ) as caller:
            # 发送（应落一条 out）
            await caller.post(
                "/v1/commands/send",
                json={"commandId": "effect-key-0000100", "contactName": "华强电子", "text": "请报价"},
            )
            # 读取（应落一条 in）
            await caller.post("/v1/commands/read", json={"contactName": "华强电子"})

    import asyncio

    # 同步执行异步测试体
    asyncio.run(run())
    # 重开 store 读取落库结果
    store = ChatHistoryStore(chat_path)
    history = store.get_history("华强电子")
    # 共 2 条
    assert len(history) == 2
    # 第一条是发送（out，带 commandId）
    assert history[0]["direction"] == "out" and history[0]["commandId"] == "effect-key-0000100"
    # 第二条是读取（in，文本"有货"）
    assert history[1]["direction"] == "in" and history[1]["text"] == "有货"


def test_chat_history_query_auto_updates(tmp_path):
    """查询聊天记录：先自动更新一次（读取落库），再返回历史。"""
    # 临时聊天库
    chat_path = tmp_path / "chat.sqlite3"
    # 应用工厂
    app = create_app(
        _RecordingAutomation(), token="test-token",
        ledger_path=tmp_path / "ledger.sqlite3", chat_history_path=chat_path,
    )

    async def run():
        # 查询聊天记录
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://qq-service.test",
            headers={"Authorization": "Bearer test-token"},
        ) as caller:
            return await caller.post("/v1/chat/history", json={"contactName": "华强电子"})

    import asyncio

    # 首次查询：触发自动更新
    response = asyncio.run(run())
    # 200
    assert response.status_code == 200
    body = response.json()
    # 更新成功
    assert body["update"] == "ok"
    # 1 条消息
    assert body["count"] == 1
    # 方向为 in，文本"有货"
    assert body["messages"][0]["direction"] == "in"
    assert body["messages"][0]["text"] == "有货"
    # 已落库：再次查询不重复入库（去重生效）
    response = asyncio.run(run())
    assert response.json()["count"] == 1


def test_chat_history_query_update_failure_keeps_history(tmp_path):
    """自动更新失败（QQ 读取异常）不阻断历史查询，update=failed。"""
    # 临时聊天库
    chat_path = tmp_path / "chat.sqlite3"
    # 先预置一条历史消息
    store = ChatHistoryStore(chat_path)
    store.append("华强电子", "out", "历史消息", "effect-key-0000200")
    # 应用工厂（读取必失败）
    app = create_app(
        _ReadFailAutomation(), token="test-token",
        ledger_path=tmp_path / "ledger.sqlite3", chat_history_path=chat_path,
    )

    async def run():
        # 查询聊天记录
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://qq-service.test",
            headers={"Authorization": "Bearer test-token"},
        ) as caller:
            return await caller.post("/v1/chat/history", json={"contactName": "华强电子"})

    import asyncio

    # 查询（自动更新会失败）
    response = asyncio.run(run())
    # 仍 200（不阻断）
    assert response.status_code == 200
    body = response.json()
    # 更新标记 failed
    assert body["update"] == "failed"
    # 历史仍在
    assert body["count"] == 1
    assert body["messages"][0]["text"] == "历史消息"
