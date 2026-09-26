"""把既有 QQ 实现层收敛为 Service 内部业务端口。"""

from __future__ import annotations

from typing import Protocol


class QqAutomationError(RuntimeError):
    """可安全投影为公开 API 错误的 QQ 自动化失败。"""


class QqAutomationPort(Protocol):
    def list_contacts(self) -> dict[str, dict]: ...

    def send_message(self, contact_name: str, text: str) -> dict: ...

    def read_messages(self, contact_name: str) -> dict: ...


class LegacyQqAutomationFacade:
    """复用既有 QQAtomicOperator FIFO，不改变 WGC、OCR 或输入实现。"""

    def _operator(self):
        from utils.qq.QQAtomicOperator import QQAtomicOperator

        return QQAtomicOperator()

    def list_contacts(self) -> dict[str, dict]:
        contacts, error = self._operator().get_contact_list(timeout=60.0)
        if error is not None:
            raise QqAutomationError("QQ_CONTACTS_UNAVAILABLE") from error
        return contacts

    def send_message(self, contact_name: str, text: str) -> dict:
        sent, error = self._operator().send_message(contact_name, text, timeout=60.0)
        if error is not None:
            raise QqAutomationError("QQ_SEND_FAILED") from error
        return {"ok": bool(sent), "sent": bool(sent), "textLength": len(text), "error": None}

    def read_messages(self, contact_name: str) -> dict:
        messages, error = self._operator().read_message_list(contact_name, timeout=60.0)
        if error is not None:
            raise QqAutomationError("QQ_MESSAGES_UNAVAILABLE") from error
        payload = [
            {
                "text": item["text"],
                "isSelf": item["is_self"],
                "x": item["rect"][0],
                "y": item["rect"][1],
                "w": item["rect"][2],
                "h": item["rect"][3],
            }
            for item in messages
        ]
        return {"ok": True, "messages": payload, "count": len(payload), "error": None}
