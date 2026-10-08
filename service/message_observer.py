"""QQ 服务内部观察已订阅会话；主应用只收回调，不进行 GUI 或 LLM 轮询。"""

from __future__ import annotations

import asyncio
import logging

from .facade import QqAutomationError

logger = logging.getLogger("qq_service.observer")


class message_observer:
    """持久消息先重放到 outbox，再采集新快照，消除两份 SQLite 提交之间的丢信窗口。"""

    def __init__(self, bound_automation, *, subscriptions_provider, publish_received_message,
                 publish_identity_invalidated=None) -> None:
        self._automation = bound_automation
        self._subscriptions = subscriptions_provider
        self._publish = publish_received_message
        self._invalidate = publish_identity_invalidated
        self._stop = asyncio.Event()
        self._task = None
        self._cursors: dict[tuple[str, str], int] = {}
        self._subscription_groups: dict[tuple[str, str], tuple[str, ...]] = {}

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            # 在途 GUI 读取必须先退出，不能取消 to_thread 后放任后台继续抢窗口。
            await self._task
            self._task = None

    def _publish_history(self, subscription: dict) -> None:
        key = (subscription["sessionId"], subscription["conversationId"])
        group = tuple(subscription["subscriptionIds"])
        if self._subscription_groups.get(key) != group:
            self._cursors[key] = subscription["afterSequence"]
            self._subscription_groups[key] = group
        cursor = self._cursors.get(key, subscription["afterSequence"])
        while messages := self._automation.bound_history(*key, cursor):
            for message in messages:
                self._publish(*key, message)
                cursor = message["sequence"]
                self._cursors[key] = cursor

    async def observe_once(self) -> None:
        subscriptions = self._subscriptions()
        active = {(item["sessionId"], item["conversationId"]) for item in subscriptions}
        self._cursors = {key: value for key, value in self._cursors.items() if key in active}
        self._subscription_groups = {key: value for key, value in self._subscription_groups.items() if key in active}
        if not subscriptions:
            return
        for subscription in subscriptions:
            await asyncio.to_thread(self._publish_history, subscription)
        contacts = await asyncio.to_thread(self._automation.list_bound_contacts)
        for subscription in subscriptions:
            if self._stop.is_set():
                return
            contact = contacts.get(subscription["conversationId"])
            if contact is None or contact["sessionId"] != subscription["sessionId"]:
                if contacts and not any(item["sessionId"] == subscription["sessionId"] for item in contacts.values()):
                    self._invalidate_session(subscription, "QQ_IDENTITY_SESSION_CHANGED")
                continue
            # 已激活会话不会显示红点，仍需读取；其余仅在出现未读提示时采集。
            if contact.get("new_msg") or contact.get("active"):
                try:
                    await asyncio.to_thread(self._automation.read_bound_messages, contact["name"],
                                            subscription["sessionId"], subscription["conversationId"])
                    await asyncio.to_thread(self._publish_history, subscription)
                except (QqAutomationError, ValueError) as exc:
                    code = str(exc)
                    if code in {"QQ_IDENTITY_SESSION_CHANGED", "QQ_IDENTITY_HEADER_CHANGED",
                                "QQ_IDENTITY_HEADER_MISMATCH", "QQ_IDENTITY_CONTACT_CHANGED",
                                "QQ_IDENTITY_CHANGED_DURING_READ"}:
                        self._invalidate_session(subscription, code)
                    logger.warning("QQ会话采集未完成 conversation_id=%s error=%s", subscription["conversationId"], exc)

    def _invalidate_session(self, subscription: dict, code: str) -> None:
        if self._invalidate is not None:
            self._invalidate(subscription["sessionId"], subscription["conversationId"], code)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.observe_once()
            except (QqAutomationError, ValueError) as exc:
                logger.warning("QQ订阅采集未完成: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
