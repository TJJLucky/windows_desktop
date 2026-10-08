"""QQ 身份扩展的原子界面操作；必须由 Dispatcher 单消费者调用。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _avatar_digest(image, avatar: tuple[int, int, int]) -> str:
    """只保留头像圆内部，避免激活行背景、红点及移动坐标改变身份。"""
    cx, cy, radius = avatar
    inset = max(1, int(radius * 0.55))
    pixels = image.crop((cx - inset, cy - inset, cx + inset, cy + inset)).convert("RGB").resize((12, 12))
    quantized = bytes(channel // 16 for pixel in pixels.getdata() for channel in pixel)
    return hashlib.sha256(quantized).hexdigest()


class bound_operations:
    """联系人名称与头像锚定可见行；发送前核验读取阶段保存的完整标题。"""

    def __init__(self, user_list, input_box, message_list) -> None:
        self.users = user_list
        self.input = input_box
        self.messages = message_list

    def _session_evidence(self) -> dict:
        import psutil
        import win32process

        region = self.users.userList_region
        # 导航栏在联系人列表左侧，最上方头像是当前账号的界面证据。
        candidates = [circle for circle in self.users.detect_avatar_circles(region.full_image)
                      if circle[0] < region.image_region["left"] and circle[1] < region.image_region["top"]]
        if not candidates:
            raise ValueError("QQ_ACCOUNT_AVATAR_EVIDENCE_REQUIRED")
        account_avatar = min(candidates, key=lambda circle: circle[1])
        _, pid = win32process.GetWindowThreadProcessId(self.users.hwnd)
        return {"pid": pid, "processStartedAt": psutil.Process(pid).create_time(),
                "hwnd": self.users.hwnd, "accountAvatarDigest": _avatar_digest(region.full_image, account_avatar)}

    def snapshot(self) -> dict:
        self.users._ensure_user_list_fresh(force=True)
        session_evidence = self._session_evidence()
        contacts = []
        self._rows = {}
        counts = {}
        for row_name, user in self.users.identity_rows:
            evidence = {"rowName": row_name, "avatarDigest": _avatar_digest(self.users.userList_region.image, user.avatar)}
            key = _digest(evidence)
            counts[key] = counts.get(key, 0) + 1
            self._rows[key] = user
            contacts.append({**user.to_dict(), "name": row_name,
                             "active": self.users.is_active_bg(self.users.get_user_image(user.rect, user.avatar)),
                             "sourceKey": key, "evidence": evidence})
        ambiguous = {item["sourceKey"]: {"name": item["name"], "evidence": item["evidence"],
                                         "count": counts[item["sourceKey"]]}
                     for item in contacts if counts[item["sourceKey"]] > 1}
        self._rows = {key: value for key, value in self._rows.items() if key not in ambiguous}
        return {"sessionKey": _digest(session_evidence), "sessionEvidence": session_evidence,
                "contacts": [item for item in contacts if item["sourceKey"] not in ambiguous],
                "ambiguousContacts": list(ambiguous.values())}

    def _activate(self, expected: dict) -> str:
        snapshot = self.snapshot()
        if snapshot["sessionKey"] != expected["sessionKey"]:
            raise ValueError("QQ_IDENTITY_SESSION_CHANGED")
        user = self._rows.get(expected["sourceKey"])
        if user is None:
            raise ValueError("QQ_IDENTITY_CONTACT_CHANGED")
        self._selected_user = user
        # 不走旧的双向包含名字匹配；直接点击经过头像校验的唯一可见行。
        self.users.active_user(replace(user, active=False))
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if not self._selected_row_matches(expected):
                time.sleep(0.1)
                continue
            _, header_name = self.users.get_user_list_top_right_ocr()
            if header_name and (expected["headerName"] is None or header_name == expected["headerName"]):
                # 标题必须与点击行的完整 OCR 文本相容，截断只允许前缀；再验证选中行背景。
                row_name = expected["evidence"]["rowName"]
                if not header_name.startswith(row_name):
                    raise ValueError("QQ_IDENTITY_HEADER_MISMATCH")
                return header_name
            time.sleep(0.1)
        raise ValueError("QQ_IDENTITY_HEADER_NOT_CONFIRMED")

    def _selected_row_matches(self, expected: dict) -> bool:
        # 点击可能恰遇列表重排，重新识别全部行，不能沿用旧坐标或同名默认头像。
        snapshot = self.snapshot()
        if snapshot["sessionKey"] != expected["sessionKey"]:
            raise ValueError("QQ_IDENTITY_SESSION_CHANGED")
        user = self._rows.get(expected["sourceKey"])
        if user is None:
            raise ValueError("QQ_IDENTITY_CONTACT_CHANGED")
        self._selected_user = user
        return self.users.is_active_bg(self.users.get_user_image(user.rect, user.avatar))

    def execute(self, operation: str, expected: dict | None = None, text: str | None = None) -> dict:
        if operation == "list":
            return self.snapshot()
        if expected is None:
            raise ValueError("QQ_IDENTITY_BINDING_REQUIRED")
        header_name = self._activate(expected)
        if operation == "read":
            from .copied_messages import parse_copied_messages
            copied = self.messages.copy_visible_selection()
            selected = self._selected_row_matches(expected)
            _, current_header = self.users.get_user_list_top_right_ocr()
            if current_header != header_name or not selected:
                raise ValueError("QQ_IDENTITY_CHANGED_DURING_READ")
            return {"headerName": header_name, "messages": parse_copied_messages(copied)}
        if operation == "send":
            if not expected["headerName"] or not text:
                raise ValueError("QQ_IDENTITY_READ_BASELINE_REQUIRED")
            self.input.refresh()
            if self.input.read_draft_text():
                raise ValueError("QQ_EXISTING_DRAFT")
            self.input.paste_text(text)
            if self.input.read_draft_text() != text:
                raise ValueError("QQ_DRAFT_CONTENT_MISMATCH")
            # 粘贴后再次读取标题，若用户手工切走会话则拒绝按发送键。
            selected = self._selected_row_matches(expected)
            _, current_header = self.users.get_user_list_top_right_ocr()
            if current_header != header_name or not selected:
                raise ValueError("QQ_IDENTITY_CHANGED_BEFORE_SEND")
            self.input.send_message()
            return {"ok": True, "sent": True, "textLength": len(text), "error": None}
        raise ValueError("QQ_BOUND_OPERATION_INVALID")
