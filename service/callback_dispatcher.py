"""服务内的持久命令消费与 outbox 投递，不运行 LLM 或询价决策。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import httpx

from .callback_store import IDENTITY_INVALIDATION_ERRORS, callback_store
from .facade import QqAutomationError, QqContactNotFoundError


logger = logging.getLogger("qq_service.callbacks")


class callback_dispatcher:
    """命令只由单消费者执行；重试只投递事件，从不重发结果未知的命令。"""

    def __init__(self, store: callback_store, automation, *, transport=None):
        self.store = store
        self.automation = automation
        self.transport = transport
        self._commands_ready = asyncio.Event()
        self._events_ready = asyncio.Event()
        self._loop = None
        self._tasks = []

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self._commands_ready.set()
        self._events_ready.set()
        self._tasks = [asyncio.create_task(self._consume_commands()), asyncio.create_task(self._deliver_events())]

    async def close(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._loop = None

    def notify(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._commands_ready.set)
            self._loop.call_soon_threadsafe(self._events_ready.set)

    def publish_received_message(self, session_id, conversation_id, message):
        self.store.publish_received_message(session_id, conversation_id, message)
        self.notify()

    def publish_identity_invalidated(self, session_id, conversation_id, error_code):
        self.store.publish_identity_invalidated(session_id, conversation_id, error_code)
        self.notify()

    async def process_next_command(self) -> bool:
        request = self.store.next_command()
        if request is None:
            return False
        command_id = request["commandId"]
        try:
            result = await asyncio.to_thread(
                self.automation.send_bound_message,
                request["contactName"], request["text"], request["sessionId"], request["conversationId"],
            )
            status = "SUCCEEDED" if result.get("sent") is True else "FAILED"
        except asyncio.CancelledError:
            self.store.resolve_command(command_id, "EFFECT_UNKNOWN", {
                "ok": False, "sent": False, "error": "QQ_SERVICE_STOPPED_DURING_SEND",
            })
            raise
        except QqContactNotFoundError:
            status, result = "FAILED", {"ok": False, "sent": False, "error": "QQ_CONTACT_NOT_FOUND"}
        except QqAutomationError as exc:
            code = str(exc)
            if code in IDENTITY_INVALIDATION_ERRORS:
                # 这些核验发生在按发送键之前，明确拒绝和已发送后结果未知分开记录。
                status, result = "FAILED", {"ok": False, "sent": False, "error": code}
                self.publish_identity_invalidated(request["sessionId"], request["conversationId"], code)
            elif code in {"QQ_EXISTING_DRAFT", "QQ_DRAFT_NON_TEXT", "QQ_DRAFT_CONTENT_MISMATCH"}:
                status, result = "FAILED", {"ok": False, "sent": False, "error": code}
            else:
                status, result = "EFFECT_UNKNOWN", {"ok": False, "sent": False, "error": "QQ_SEND_RESULT_UNKNOWN"}
        except Exception as exc:
            # 跨过执行边界后只能报告未知，不把异常当成可以重发的依据。
            logger.warning("异步 QQ 命令结果未确认 command_id=%s error_type=%s", command_id, type(exc).__name__)
            status, result = "EFFECT_UNKNOWN", {"ok": False, "sent": False, "error": "QQ_SEND_RESULT_UNKNOWN"}
        self.store.resolve_command(command_id, status, result)
        self._events_ready.set()
        return True

    async def _consume_commands(self):
        while True:
            await self._commands_ready.wait()
            self._commands_ready.clear()
            while await self.process_next_command():
                pass

    async def deliver_pending(self) -> int:
        delivered = 0
        async with httpx.AsyncClient(transport=self.transport, timeout=5, trust_env=False, follow_redirects=False) as http:
            for event in self.store.pending_events():
                accepted = False
                try:
                    response = await http.post(
                        event["callback_url"], json=json.loads(event["payload_json"]),
                        headers={"Authorization": "Bearer " + event["callback_token"]},
                    )
                    if response.status_code == 200:
                        body = response.json()
                        accepted = isinstance(body, dict) and body.get("acknowledgedEventId") == event["event_id"]
                except (httpx.HTTPError, ValueError):
                    accepted = False
                if accepted:
                    self.store.acknowledge(event["event_id"])
                    delivered += 1
                else:
                    self.store.retry_later(event["event_id"], event["attempts"])
        return delivered

    async def _deliver_events(self):
        while True:
            # 仅服务负责 outbox 的退避重投；Runtime 不轮询 QQ 或占用 LLM 执行槽。
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._events_ready.wait(), timeout=1)
            self._events_ready.clear()
            await self.deliver_pending()
