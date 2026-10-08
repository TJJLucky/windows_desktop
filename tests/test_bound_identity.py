"""持久身份、复制消息游标与观察者测试；不连接真实 QQ。"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from PIL import Image

from service.chat_history import ChatHistoryStore
from service.facade import QqAutomationError, identity_facade
from service.identity_store import identity_store
from service.message_observer import message_observer


def message(text="有货", timestamp="10-08 12:00:01", sender="商家"):
    return {"text": text, "timestamp": timestamp, "sender": sender, "rawText": f"{sender}: {timestamp} {text}"}


class raw_automation:
    def __init__(self):
        self.snapshot = {"sessionKey": "process-account-a", "sessionEvidence": {"pid": 42}, "contacts": [
            {"sourceKey": "merchant-avatar-a", "name": "华强电子", "evidence": {"rowName": "华强电子", "avatarDigest": "a"},
             "active": True, "new_msg": False}]}
        self.messages = [message()]
        self.header = "华强电子"
        self.sent = []

    def identity_snapshot(self):
        return copy.deepcopy(self.snapshot)

    def bound_read(self, expected):
        assert expected["sessionKey"] == self.snapshot["sessionKey"]
        return {"headerName": self.header, "messages": list(self.messages)}

    def bound_send(self, expected, text):
        self.sent.append((expected, text))
        return {"ok": True, "sent": True, "error": None, "textLength": len(text)}


def bound(tmp_path, raw):
    return identity_facade(raw, tmp_path / "identities.db", ChatHistoryStore(tmp_path / "history.db"))


def test_identity_survives_service_restart_but_not_account_or_avatar_change(tmp_path):
    raw = raw_automation()
    first = next(iter(bound(tmp_path, raw).list_bound_contacts().values()))
    again = next(iter(bound(tmp_path, raw).list_bound_contacts().values()))
    assert (again["sessionId"], again["conversationId"]) == (first["sessionId"], first["conversationId"])
    raw.snapshot["contacts"][0]["sourceKey"] = "changed-avatar"
    changed = next(iter(bound(tmp_path, raw).list_bound_contacts().values()))
    assert changed["conversationId"] != first["conversationId"]
    raw.snapshot["sessionKey"] = "other-account-process"
    other = next(iter(bound(tmp_path, raw).list_bound_contacts().values()))
    assert other["sessionId"] != first["sessionId"]


def test_same_name_contacts_keep_separate_ids_and_duplicate_evidence_rejected(tmp_path):
    raw = raw_automation()
    raw.snapshot["contacts"].append({**raw.snapshot["contacts"][0], "sourceKey": "other-avatar"})
    assert len(bound(tmp_path, raw).list_bound_contacts()) == 2
    raw.snapshot["contacts"].append(dict(raw.snapshot["contacts"][0]))
    with pytest.raises(QqAutomationError, match="AMBIGUOUS"):
        bound(tmp_path, raw).list_bound_contacts()


def test_read_baseline_required_and_cross_contact_identity_cannot_send(tmp_path):
    raw = raw_automation()
    adapter = bound(tmp_path, raw)
    contact = next(iter(adapter.list_bound_contacts().values()))
    ids = contact["sessionId"], contact["conversationId"]
    with pytest.raises(QqAutomationError, match="BASELINE_REQUIRED"):
        adapter.send_bound_message(contact["name"], "报价", *ids)
    snapshot = adapter.read_bound_messages(contact["name"], *ids)
    assert snapshot["sequence"] == 1
    assert snapshot["messages"][0]["isSelf"] is None
    assert snapshot["messages"][0]["direction"] == "UNKNOWN"
    assert adapter.send_bound_message(contact["name"], "报价", *ids)["sent"]
    with pytest.raises(QqAutomationError, match="MISMATCH"):
        adapter.send_bound_message("不同商家", "报价", *ids)
    raw.header = "相似但不同的商家"
    with pytest.raises(QqAutomationError, match="HEADER_CHANGED"):
        adapter.read_bound_messages(contact["name"], *ids)
    assert len(raw.sent) == 1


def test_bound_history_preserves_same_text_occurrences_and_restart_ids(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.db")
    identical = message()
    store.append_bound_visible("s", "c", [identical])
    first = store.get_bound_history("s", "c")
    store.append_bound_visible("s", "c", [identical, identical])
    twice = store.get_bound_history("s", "c")
    assert len(twice) == 2
    assert twice[0] == first[0]
    assert twice[0]["messageId"] != twice[1]["messageId"]
    store = ChatHistoryStore(tmp_path / "history.db")
    store.append_bound_visible("s", "c", [identical, identical])
    assert store.get_bound_history("s", "c") == twice
    store.append_bound_visible("other-session", "c", [identical])
    assert store.get_bound_history("other-session", "c")[0]["messageId"] != first[0]["messageId"]


def test_scrolled_history_does_not_become_new_reply(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.db")
    values = [message(str(number), f"10-08 12:00:0{number}") for number in range(4)]
    store.append_bound_visible("s", "c", values[:3])
    store.append_bound_visible("s", "c", values[1:])
    assert store.bound_sequence("s", "c") == 4


def test_history_backscroll_then_new_tail_keeps_original_ids(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.db")
    values = [message(str(number), f"10-08 12:00:0{number}") for number in range(4)]
    store.append_bound_visible("s", "c", values[:3])
    first_ids = [row["messageId"] for row in store.get_bound_history("s", "c")]
    store.append_bound_visible("s", "c", values[:2])
    store.append_bound_visible("s", "c", values[1:])
    history = store.get_bound_history("s", "c")
    assert len(history) == 4
    assert [row["messageId"] for row in history[:3]] == first_ids
    store.append_bound_visible("s", "c", values[1:3])
    assert store.bound_sequence("s", "c") == 4


@pytest.mark.asyncio
async def test_observer_replays_persisted_before_crash_and_new_subscription(tmp_path):
    raw = raw_automation()
    adapter = bound(tmp_path, raw)
    contact = next(iter(adapter.list_bound_contacts().values()))
    ids = contact["sessionId"], contact["conversationId"]
    adapter.read_bound_messages(contact["name"], *ids)
    subscriptions = [{"sessionId": ids[0], "conversationId": ids[1], "afterSequence": 0, "subscriptionIds": ["one"]}]
    published = []
    observer = message_observer(adapter, subscriptions_provider=lambda: subscriptions,
                                publish_received_message=lambda s, c, value: published.append(value))
    await observer.observe_once()
    assert len(published) == 1
    await observer.observe_once()
    assert len(published) == 1
    subscriptions[0]["subscriptionIds"] = ["one", "two"]
    await observer.observe_once()
    assert len(published) == 2
    assert published[0]["messageId"] == published[1]["messageId"]


@pytest.mark.asyncio
async def test_observer_failure_does_not_starve_other_merchants_and_ignores_unsubscribed(tmp_path):
    raw = raw_automation()
    raw.snapshot["contacts"].append({**raw.snapshot["contacts"][0], "sourceKey": "other-avatar", "name": "商家二"})
    adapter = bound(tmp_path, raw)
    contacts = list(adapter.list_bound_contacts().values())
    subscriptions = [{"sessionId": c["sessionId"], "conversationId": c["conversationId"],
                      "afterSequence": 0, "subscriptionIds": [str(i)]} for i, c in enumerate(contacts)]
    actual_read = adapter.read_bound_messages
    reads = []

    def read(name, session_id, conversation_id):
        reads.append(name)
        if name == "华强电子":
            raise QqAutomationError("QQ_MESSAGES_UNAVAILABLE")
        return actual_read(name, session_id, conversation_id)

    adapter.read_bound_messages = read
    published = []
    observer = message_observer(adapter, subscriptions_provider=lambda: subscriptions,
                                publish_received_message=lambda s, c, value: published.append(value))
    await observer.observe_once()
    assert reads == ["华强电子", "商家二"]
    assert len(published) == 1
    subscriptions.clear()
    await observer.observe_once()
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_observer_account_change_emits_invalidation(tmp_path):
    raw = raw_automation()
    adapter = bound(tmp_path, raw)
    contact = next(iter(adapter.list_bound_contacts().values()))
    subscriptions = [{**contact, "afterSequence": 0, "subscriptionIds": ["one"]}]
    raw.snapshot["sessionKey"] = "other-account"
    invalidated = []
    observer = message_observer(adapter, subscriptions_provider=lambda: subscriptions,
                                publish_received_message=lambda *args: None,
                                publish_identity_invalidated=lambda *args: invalidated.append(args))
    await observer.observe_once()
    assert invalidated == [(contact["sessionId"], contact["conversationId"], "QQ_IDENTITY_SESSION_CHANGED")]


def test_raw_send_verifies_avatar_and_header_after_paste(monkeypatch):
    from utils.qq.bound_operations import _avatar_digest, bound_operations
    from utils.qq.models import User

    pixels = Image.new("RGB", (100, 100), (80, 90, 100))
    user = User(name="华强电子", avatar=(30, 30, 10), rect=(0, 0, 90, 60))
    users = SimpleNamespace(identity_rows=[("华强电子", user)],
                            userList_region=SimpleNamespace(image=pixels),
                            _ensure_user_list_fresh=lambda **kwargs: None,
                            active_user=lambda selected: None,
                            refresh_image=lambda: None,
                            get_user_list_top_right_ocr=lambda: (True, "华强电子"),
                            get_user_image=lambda *args: pixels,
                            is_active_bg=lambda image: True)
    sends = []
    input_box = SimpleNamespace(refresh=lambda: None, draft="", send_message=lambda: sends.append(True))
    input_box.paste_text = lambda text: setattr(input_box, "draft", text)
    input_box.read_draft_text = lambda: input_box.draft
    operations = bound_operations(users, input_box, None)
    monkeypatch.setattr(operations, "_session_evidence", lambda: {"account": "a"})
    snapshot = operations.snapshot()
    expected = {"sessionKey": snapshot["sessionKey"], **snapshot["contacts"][0], "headerName": "华强电子"}
    assert operations.execute("send", expected, "报价")["sent"]
    assert sends == [True]

    def switch_during_paste(text):
        input_box.draft = text
        users.get_user_list_top_right_ocr = lambda: (True, "相似商家")

    input_box.paste_text = switch_during_paste
    input_box.draft = ""
    with pytest.raises(ValueError, match="CHANGED_BEFORE_SEND"):
        operations.execute("send", expected, "再次报价")
    assert sends == [True]


def test_raw_snapshot_excludes_ambiguous_rows_without_blocking_other_contacts(monkeypatch):
    from utils.qq.bound_operations import bound_operations
    from utils.qq.models import User

    pixels = Image.new("RGB", (100, 100), (80, 90, 100))
    users = SimpleNamespace(identity_rows=[("同名", User(name="同名", avatar=(20, 20, 10))),
                                           ("同名", User(name="同名", avatar=(20, 60, 10))),
                                           ("唯一", User(name="错误的旧canonical名", avatar=(50, 60, 10)))],
                            userList_region=SimpleNamespace(image=pixels), _ensure_user_list_fresh=lambda **kwargs: None,
                            is_active_bg=lambda image: True, get_user_image=lambda *args: pixels)
    operations = bound_operations(users, None, None)
    monkeypatch.setattr(operations, "_session_evidence", lambda: {"account": "a"})
    snapshot = operations.snapshot()
    assert [item["name"] for item in snapshot["contacts"]] == ["唯一"]
    assert snapshot["ambiguousContacts"][0]["count"] == 2
    assert len(operations._rows) == 1


@pytest.mark.parametrize("draft,pasted,error", [("用户未发草稿", None, "EXISTING_DRAFT"),
                                                  ("", "额外前缀批准文本", "CONTENT_MISMATCH")])
def test_send_rejects_user_draft_or_changed_authorized_content(monkeypatch, draft, pasted, error):
    from utils.qq.bound_operations import bound_operations

    sent = []
    input_box = SimpleNamespace(draft=draft, refresh=lambda: None, send_message=lambda: sent.append(True))
    input_box.read_draft_text = lambda: input_box.draft
    input_box.paste_text = lambda text: setattr(input_box, "draft", pasted)
    operations = bound_operations(None, input_box, None)
    monkeypatch.setattr(operations, "_activate", lambda expected: "商家")
    with pytest.raises(ValueError, match=error):
        operations.execute("send", {"headerName": "商家"}, "批准文本")
    assert sent == []
    assert input_box.draft == (draft if draft else pasted)


def test_list_reordering_with_same_avatar_never_binds_similar_other_row(monkeypatch):
    from utils.qq import bound_operations as module
    from utils.qq.bound_operations import bound_operations
    from utils.qq.models import User

    pixels = Image.new("RGB", (100, 100), (80, 90, 100))
    first = User(name="ABC", avatar=(20, 20, 10), rect=(0, 0, 90, 40))
    other = User(name="ABCshop", avatar=(20, 60, 10), rect=(0, 40, 90, 80))
    users = SimpleNamespace(identity_rows=[("ABC", first), ("ABCshop", other)],
                            userList_region=SimpleNamespace(image=pixels),
                            _ensure_user_list_fresh=lambda **kwargs: None,
                            get_user_image=lambda rect, avatar: rect,
                            is_active_bg=lambda rect: rect[1] == 0,
                            get_user_list_top_right_ocr=lambda: (True, "ABCshop"))

    def reordered_click(selected):
        users.identity_rows = [("ABCshop", first), ("ABC", other)]

    users.active_user = reordered_click
    operations = bound_operations(users, None, None)
    monkeypatch.setattr(operations, "_session_evidence", lambda: {"account": "a"})
    snapshot = operations.snapshot()
    expected = {"sessionKey": snapshot["sessionKey"], **snapshot["contacts"][0], "headerName": None}
    ticks = iter([0.0, 0.0, 5.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", lambda value: None)
    with pytest.raises(ValueError, match="HEADER_NOT_CONFIRMED"):
        operations._activate(expected)


@pytest.mark.parametrize("extra_format", [2, 8, 17, 15])
def test_draft_copy_rejects_image_or_file_even_with_matching_unicode(monkeypatch, extra_format):
    from utils.qq import input as input_module

    clipboard = SimpleNamespace(CF_UNICODETEXT=13, OpenClipboard=lambda: None, CloseClipboard=lambda: None,
                                EmptyClipboard=lambda: None,
                                IsClipboardFormatAvailable=lambda value: value in {13, extra_format},
                                GetClipboardData=lambda value: "批准文本", CountClipboardFormats=lambda: 2)
    monkeypatch.setattr(input_module, "win32clipboard", clipboard)
    monkeypatch.setattr(input_module, "send_hotkey", lambda keys: None)
    monkeypatch.setattr(input_module.time, "sleep", lambda value: None)
    input_box = input_module.InputBox()
    monkeypatch.setattr(input_box, "click_inputbox", lambda: None)
    with pytest.raises(ValueError, match="QQ_DRAFT_NON_TEXT"):
        input_box.read_draft_text()
