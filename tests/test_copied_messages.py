from utils.qq import message as message_module
from utils.qq.copied_messages import parse_copied_messages
from utils.qq.message import MessageList


COPIED_SAMPLE = (
    "TJJ: 09-27 12:25:03 [接口压测，请忽略] 第 1 轮  "
    "TJJ: 09-27 12:25:12 [接口压测，请忽略] 第 2 轮  "
    "TJJ: 09-27 12:25:20 [接口压测，请忽略] 第 3 轮  "
    "TJJ: 09-27 12:25:28 [接口压测，请忽略] 第 4 轮  "
    "TJJ: 09-27 12:25:36 [接口压测，请忽略] 第 5 轮  "
    "TJJ: 09-27 14:52:47 [自动化全量测试 2026-09-27 14:52:42]  "
    "D_Isaac: 09-27 14:52:58 11111  "
    "TJJ: 09-27 14:53:01 [自动化全量测试 2026-09-27 14:52:57]  "
    "TJJ: 09-27 14:53:18 [自动化全量测试 2026-09-27 14:53:13]  "
    "TJJ: 09-27 19:01:59 接口发送测试20260927190152 &#x20;"
)


def test_parse_qq_copied_sample_keeps_all_messages_and_fields():
    messages = parse_copied_messages(COPIED_SAMPLE)

    assert len(messages) == 10
    assert messages[0] == {
        "sender": "TJJ",
        "timestamp": "09-27 12:25:03",
        "text": "[接口压测，请忽略] 第 1 轮",
        "rawText": "TJJ: 09-27 12:25:03 [接口压测，请忽略] 第 1 轮",
    }
    assert messages[6]["sender"] == "D_Isaac"
    assert messages[6]["text"] == "11111"
    assert messages[-1]["text"] == "接口发送测试20260927190152"


def test_parse_qq_copied_sample_supports_newline_separators():
    messages = parse_copied_messages("TJJ: 09-30 01:02:03 first\nD_Isaac: 09-30 01:02:04 second")

    assert [(item["sender"], item["text"]) for item in messages] == [
        ("TJJ", "first"),
        ("D_Isaac", "second"),
    ]


def test_message_list_integrates_activation_copy_and_parser(monkeypatch):
    calls: list[str] = []
    reader = MessageList()
    copied = "TJJ: 09-30 01:02:03 first  D_Isaac: 09-30 01:02:04 second"

    monkeypatch.setattr(message_module.UserList, "active_user_by_name", lambda _self, name: calls.append(name))
    monkeypatch.setattr(reader, "copy_visible_selection", lambda: copied)

    result = reader.read_messages("华强电子")

    assert calls == ["华强电子"]
    assert result["copiedText"] == copied
    assert [(item["sender"], item["text"]) for item in result["messages"]] == [
        ("TJJ", "first"),
        ("D_Isaac", "second"),
    ]
