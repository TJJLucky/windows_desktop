"""持久回调协议和服务队列测试；全部使用替身，不访问真实 QQ。"""

from __future__ import annotations

import json

import httpx
import pytest

from service.app import create_app


COMMAND = "command-stable-0001"


class fake_automation:
    def __init__(self):
        self.sent = []
        self.fail = False
        self.identity_warnings = []

    def list_contacts(self):
        return {"供应商": {"name": "供应商"}}

    def list_bound_contacts(self):
        return {"conversation-1": {"name": "供应商", "sessionId": "session-1", "conversationId": "conversation-1"}}

    def read_messages(self, name):
        return {"ok": True, "messages": [], "count": 0, "copiedText": "", "error": None}

    def read_bound_messages(self, name, session_id, conversation_id):
        assert (session_id, conversation_id) == ("session-1", "conversation-1")
        return {"ok": True, "sessionId": session_id, "conversationId": conversation_id,
                "sequence": 10, "messages": [], "count": 0, "error": None}

    def send_message(self, name, text):
        self.sent.append((name, text))
        return {"ok": True, "sent": True, "textLength": len(text), "error": None}

    def send_bound_message(self, name, text, session_id, conversation_id):
        assert (session_id, conversation_id) == ("session-1", "conversation-1")
        if self.fail:
            raise RuntimeError("发送是否执行无法确认")
        return self.send_message(name, text)

    def check_capture_ready(self):
        return {"ready": True}


def app_at(tmp_path, automation=None, transport=None):
    automation = automation or fake_automation()
    return create_app(automation, bound_automation=automation, ledger_path=tmp_path / "ledger.sqlite3",
                      callback_transport=transport)


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://service.test")


def subscription(*, command_id=COMMAND, suffix="command", callback_port=50123):
    body = {"schemaVersion": 1, "subscriptionId": "subscription-" + suffix, "consumerId": "runtime-1",
            "callbackUrl": f"http://127.0.0.1:{callback_port}/api/local-runtime/qq/events",
            "callbackToken": "secret-callback-token-that-is-at-least-32-characters",
            "eventTypes": ["command.completed"] if command_id else ["message.received"],
            "sessionId": "session-1", "conversationId": "conversation-1", "afterSequence": 0 if command_id else 10}
    if command_id:
        body["commandId"] = command_id
    return body


async def subscribe(caller, **kwargs):
    body = subscription(**kwargs)
    response = await caller.put("/v1/subscriptions/" + body["subscriptionId"], json=body)
    assert response.status_code == 200, response.text
    return body


def send_body():
    return {"commandId": COMMAND, "subscriptionId": "subscription-command", "sessionId": "session-1",
            "conversationId": "conversation-1", "contactName": "供应商", "text": "请报价"}


def message(identifier="message-1", sequence=11):
    return {"messageId": identifier, "sequence": sequence, "text": "含税", "isSelf": False}


@pytest.mark.asyncio
async def test_legacy_json_and_explicit_identity_extension_are_separate(tmp_path):
    app = app_at(tmp_path)
    async with client(app) as caller:
        old = await caller.post("/v1/contacts:query")
        bound = await caller.post("/v1/contacts:query?identity=true")
        read = await caller.post("/v1/commands/read", json={"contactName": "供应商", "sessionId": "session-1", "conversationId": "conversation-1"})
        capabilities = await caller.get("/v1/capabilities")
    assert old.json() == {"contacts": {"供应商": {"name": "供应商"}}, "count": 1}
    assert bound.json()["contacts"]["conversation-1"]["sessionId"] == "session-1"
    assert bound.json()["identityWarnings"] == []
    assert read.json()["sequence"] == 10
    assert capabilities.json()["capabilities"] == ["callbacks.v1", "identity.v1", "deferred-commands.v1"]


@pytest.mark.asyncio
async def test_accepted_send_is_persisted_and_executes_exactly_once(tmp_path):
    automation = fake_automation()
    app = app_at(tmp_path, automation)
    async with client(app) as caller:
        await subscribe(caller)
        accepted = await caller.post("/v1/commands/send", json=send_body())
        repeated = await caller.post("/v1/commands/send", json=send_body())
        pending = await caller.get("/v1/commands/" + COMMAND)
        assert accepted.status_code == repeated.status_code == 202
        assert pending.json()["status"] == "RUNNING"
        assert pending.json()["subscriptionId"] == "subscription-command"
        assert automation.sent == []
        assert await app.state.callbacks.process_next_command() is True
        assert await app.state.callbacks.process_next_command() is False
        done = await caller.post("/v1/commands/send", json=send_body())
        conflict = await caller.post("/v1/commands/send", json={**send_body(), "text": "不同内容"})
    assert done.status_code == 200 and done.json()["status"] == "SUCCEEDED"
    assert conflict.status_code == 409
    assert automation.sent == [("供应商", "请报价")]
    events = app.state.callback_store.pending_events()
    assert len(events) == 1
    assert json.loads(events[0]["payload_json"])["commandId"] == COMMAND


@pytest.mark.asyncio
async def test_restart_replays_only_not_started_commands_and_recovers_unknown_callback(tmp_path):
    first = app_at(tmp_path)
    async with client(first) as caller:
        await subscribe(caller)
        await caller.post("/v1/commands/send", json=send_body())
    second_automation = fake_automation()
    second = app_at(tmp_path, second_automation)
    assert await second.state.callbacks.process_next_command() is True
    assert second_automation.sent == [("供应商", "请报价")]

    other = tmp_path / "uncertain"
    other.mkdir()
    started = app_at(other)
    async with client(started) as caller:
        await subscribe(caller)
        await caller.post("/v1/commands/send", json=send_body())
    assert started.state.callback_store.next_command()["commandId"] == COMMAND
    recovered_automation = fake_automation()
    recovered = app_at(other, recovered_automation)
    assert await recovered.state.callbacks.process_next_command() is False
    assert recovered.state.callback_store.get_command(COMMAND)["status"] == "EFFECT_UNKNOWN"
    event = json.loads(recovered.state.callback_store.pending_events()[0]["payload_json"])
    assert event["status"] == "EFFECT_UNKNOWN"
    assert recovered_automation.sent == []


@pytest.mark.asyncio
async def test_outbox_requires_exact_ack_and_resubscribe_updates_port_without_losing_event(tmp_path):
    received = []
    acknowledge = False

    def receiver(request):
        received.append(request)
        body = json.loads(request.content)
        assert request.headers["Authorization"].startswith("Bearer secret-callback")
        assert "callbackToken" not in body
        return httpx.Response(200, json={"acknowledgedEventId": body["eventId"] if acknowledge else "wrong"})

    app = app_at(tmp_path, transport=httpx.MockTransport(receiver))
    async with client(app) as caller:
        await subscribe(caller, command_id=None, suffix="messages")
        app.state.callbacks.publish_received_message("session-1", "conversation-1", message())
        assert await app.state.callbacks.deliver_pending() == 0
        assert len(app.state.callback_store.pending_events(now=10**12)) == 1
        acknowledge = True
        await subscribe(caller, command_id=None, suffix="messages", callback_port=50124)
        assert await app.state.callbacks.deliver_pending() == 1
    assert len(received) == 2
    assert received[1].url.port == 50124
    assert json.loads(received[0].content)["eventId"] == json.loads(received[1].content)["eventId"]
    assert not app.state.callback_store.pending_events(now=10**12)


@pytest.mark.asyncio
async def test_message_identity_baseline_duplicates_and_unknown_direction_are_preserved(tmp_path):
    app = app_at(tmp_path)
    async with client(app) as caller:
        await subscribe(caller, command_id=None, suffix="messages")
    for item in [message("old", 10), message(), message(), message("message-2", 12)]:
        app.state.callbacks.publish_received_message("session-1", "conversation-1", item)
    unknown = {**message("unknown", 13), "isSelf": None, "sender": "QQ 昵称", "direction": "UNKNOWN", "source": "QQ_NATIVE_CLIPBOARD"}
    app.state.callbacks.publish_received_message("session-1", "conversation-1", unknown)
    events = [json.loads(row["payload_json"]) for row in app.state.callback_store.pending_events()]
    assert len(events) == 3
    assert events[-1]["message"]["isSelf"] is None
    assert events[-1]["message"]["sender"] == "QQ 昵称"
    assert len({event["message"]["messageId"] for event in events}) == 3
    with pytest.raises(ValueError):
        app.state.callbacks.publish_received_message("session-1", "conversation-1", {**message(), "isSelf": None})


@pytest.mark.asyncio
async def test_queued_cancel_and_preemptive_cancel_never_send(tmp_path):
    app = app_at(tmp_path)
    async with client(app) as caller:
        await subscribe(caller)
        await caller.post("/v1/commands/send", json=send_body())
        cancelled = await caller.request("DELETE", "/v1/commands/" + COMMAND, json={"consumerId": "runtime-1"})
        assert cancelled.json()["status"] == "CANCELLED"
        assert await app.state.callbacks.process_next_command() is False
        event = json.loads(app.state.callback_store.pending_events()[0]["payload_json"])
        assert event["status"] == "FAILED"
        future = "command-late-00001"
        await subscribe(caller, command_id=future, suffix="future")
        await caller.request("DELETE", "/v1/commands/" + future, json={"consumerId": "runtime-1"})
        rejected = await caller.post("/v1/commands/send", json={**send_body(), "commandId": future, "subscriptionId": "subscription-future"})
    assert rejected.status_code == 409
    assert app.state.bound_automation.sent == []


@pytest.mark.asyncio
async def test_cancel_cannot_claim_to_retract_running_command(tmp_path):
    app = app_at(tmp_path)
    async with client(app) as caller:
        await subscribe(caller)
        await caller.post("/v1/commands/send", json=send_body())
        app.state.callback_store.next_command()
        cancelled = await caller.request("DELETE", "/v1/commands/" + COMMAND, json={"consumerId": "runtime-1"})
    assert cancelled.json()["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_unsubscribe_keeps_existing_outbox_but_stops_new_events(tmp_path):
    app = app_at(tmp_path)
    async with client(app) as caller:
        body = await subscribe(caller, command_id=None, suffix="messages")
        app.state.callbacks.publish_received_message("session-1", "conversation-1", message())
        response = await caller.request("DELETE", "/v1/subscriptions/" + body["subscriptionId"], json={"consumerId": "runtime-1"})
        assert response.status_code == 204
    app.state.callbacks.publish_received_message("session-1", "conversation-1", message("after-stop", 12))
    assert len(app.state.callback_store.pending_events()) == 1
    assert not app.state.callback_store.active_message_subscriptions()


@pytest.mark.asyncio
async def test_subscription_rejects_remote_callback_url_and_identity_mutation(tmp_path):
    app = app_at(tmp_path)
    async with client(app) as caller:
        body = await subscribe(caller)
        altered = await caller.put("/v1/subscriptions/" + body["subscriptionId"], json={**body, "conversationId": "other"})
        remote = await caller.put("/v1/subscriptions/" + body["subscriptionId"], json={**body, "callbackUrl": "https://example.com/events"})
    assert altered.status_code == 409
    assert remote.status_code == 422


@pytest.mark.asyncio
async def test_partial_identity_send_never_falls_through_to_legacy_sender(tmp_path):
    app = app_at(tmp_path)
    async with client(app) as caller:
        payload = send_body()
        for missing in ("sessionId", "conversationId", "subscriptionId"):
            response = await caller.post("/v1/commands/send", json={key: value for key, value in payload.items() if key != missing})
            assert response.status_code == 422
    assert app.state.bound_automation.sent == []


@pytest.mark.asyncio
async def test_message_subscription_groups_include_sorted_ids_and_earliest_baseline(tmp_path):
    app = app_at(tmp_path)
    async with client(app) as caller:
        await subscribe(caller, command_id=None, suffix="z")
        earlier = {**subscription(command_id=None, suffix="a"), "afterSequence": 5}
        response = await caller.put("/v1/subscriptions/" + earlier["subscriptionId"], json=earlier)
        assert response.status_code == 200
    assert app.state.callback_store.active_message_subscriptions() == [{
        "sessionId": "session-1", "conversationId": "conversation-1", "afterSequence": 5,
        "subscriptionIds": ["subscription-a", "subscription-z"],
    }]


@pytest.mark.asyncio
async def test_identity_invalidation_is_durable_deduplicated_and_stops_new_observation(tmp_path):
    app = app_at(tmp_path)
    async with client(app) as caller:
        await subscribe(caller, command_id=None, suffix="messages")
    for _ in range(2):
        app.state.callbacks.publish_identity_invalidated("session-1", "conversation-1", "QQ_IDENTITY_SESSION_CHANGED")
    event = json.loads(app.state.callback_store.pending_events()[0]["payload_json"])
    assert event["eventType"] == "conversation.invalidated"
    assert event["reason"] == event["errorCode"] == "QQ_IDENTITY_SESSION_CHANGED"
    assert "message" not in event
    assert not app.state.callback_store.active_message_subscriptions()
    assert len(app_at(tmp_path).state.callback_store.pending_events()) == 1
    with pytest.raises(ValueError):
        app.state.callbacks.publish_identity_invalidated("session-1", "conversation-1", "QQ_WINDOW_NOT_READY")


@pytest.mark.asyncio
async def test_identity_rejection_before_send_fails_command_and_invalidates_subscription(tmp_path):
    from service.facade import QqAutomationError

    class changed_automation(fake_automation):
        def send_bound_message(self, *args):
            raise QqAutomationError("QQ_IDENTITY_CHANGED_BEFORE_SEND")

    app = app_at(tmp_path, changed_automation())
    async with client(app) as caller:
        await subscribe(caller, command_id=None, suffix="messages")
        await subscribe(caller)
        await caller.post("/v1/commands/send", json=send_body())
    await app.state.callbacks.process_next_command()
    events = [json.loads(row["payload_json"]) for row in app.state.callback_store.pending_events()]
    assert {event["eventType"] for event in events} == {"command.completed", "conversation.invalidated"}
    result = app.state.callback_store.get_command(COMMAND)
    assert result["status"] == "FAILED"
    assert result["result"]["error"] == "QQ_IDENTITY_CHANGED_BEFORE_SEND"
    assert app.state.bound_automation.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code,expected_status", [
    ("QQ_EXISTING_DRAFT", "FAILED"), ("QQ_DRAFT_NON_TEXT", "FAILED"),
    ("QQ_DRAFT_CONTENT_MISMATCH", "FAILED"), ("QQ_GUI_TIMEOUT", "EFFECT_UNKNOWN"),
])
async def test_draft_rejections_are_known_failures_but_gui_timeout_stays_unknown(tmp_path, error_code, expected_status):
    from service.facade import QqAutomationError

    class rejected_automation(fake_automation):
        def send_bound_message(self, *args):
            raise QqAutomationError(error_code)

    app = app_at(tmp_path, rejected_automation())
    async with client(app) as caller:
        await subscribe(caller, command_id=None, suffix="messages")
        await subscribe(caller)
        await caller.post("/v1/commands/send", json=send_body())
    await app.state.callbacks.process_next_command()
    result = app.state.callback_store.get_command(COMMAND)
    assert result["status"] == expected_status
    assert app.state.callback_store.active_message_subscriptions()
    assert app.state.bound_automation.sent == []


@pytest.mark.asyncio
async def test_lifespan_runs_queue_and_shuts_down_without_qq_test_side_effects(tmp_path):
    delivered = []

    def receiver(request):
        payload = json.loads(request.content)
        delivered.append(payload)
        return httpx.Response(200, json={"acknowledgedEventId": payload["eventId"]})

    app = app_at(tmp_path, transport=httpx.MockTransport(receiver))
    async with app.router.lifespan_context(app):
        async with client(app) as caller:
            await subscribe(caller)
            await caller.post("/v1/commands/send", json=send_body())
            import asyncio
            for _ in range(100):
                if delivered:
                    break
                await asyncio.sleep(.01)
    assert delivered and delivered[0]["status"] == "SUCCEEDED"
    assert app.state.bound_automation.sent == [("供应商", "请报价")]
