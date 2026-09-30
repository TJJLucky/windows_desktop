"""Parse QQ native copied chat text into message records."""

from __future__ import annotations

import html
import re


_MESSAGE_HEADER = re.compile(
    # QQ 在同一行拼接消息时通常使用两个以上空格；换行也作为合法边界。
    r"(?:^|\n| {2,})(?P<sender>[^:\r\n]+?):\s*"
    r"(?P<timestamp>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
    r"(?:\s+|$)"
)


def parse_copied_messages(copied_text: str) -> list[dict[str, str]]:
    """Split QQ clipboard text while preserving sender, time, body, and raw text."""
    text = html.unescape(str(copied_text or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").strip()
    if not text:
        raise ValueError("QQ_COPIED_MESSAGE_EMPTY")

    matches = list(_MESSAGE_HEADER.finditer(text))
    if not matches:
        raise ValueError("QQ_COPIED_MESSAGE_FORMAT_INVALID")

    messages: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw = text[match.start():end].strip()
        body = text[match.end():end].strip()
        if not body:
            continue
        messages.append(
            {
                "sender": match.group("sender").strip(),
                "timestamp": match.group("timestamp").strip(),
                "text": body,
                "rawText": raw,
            }
        )
    if not messages:
        raise ValueError("QQ_COPIED_MESSAGE_EMPTY")
    return messages
