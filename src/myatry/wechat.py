"""Direct WeChat reader/native transport for the durable Bridge outbox."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time

import httpx

from myatri.contracts import digest
from .bridge import Incoming
from .server import pending_deliveries
from .wechat_client import NotReady, incoming

logger = logging.getLogger(__name__)


def delivery_error_code(error, stage):
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if stage == "ack":
        return "ack_error"
    if isinstance(error, NotReady):
        return {"SEND_RECEIPT_UNKNOWN": "receipt_timeout",
                "NATIVE_SEND_REJECTED": "native_rejected"}.get(str(error), "binding_unavailable")
    return "transport_error" if isinstance(error, httpx.HTTPError) else "send_error"


class WeChatRunner:
    def __init__(self, bridge, client, *, platform_id="xiaowennuan-wechat", observe_only=False):
        self.bridge = bridge
        self.client = client
        self.platform_id = platform_id
        self.observe_only = observe_only
        self.started_at = int(time.time())
        self.scope = None
        self.stopped = False
        self.tasks = set()
        self.poll_task = None
        self.inflight = set()
        self.delivery_lock = asyncio.Lock()

    def message(self, payload):
        item = incoming(payload, self.client, self.started_at)
        if item is None:
            return None
        route = f"{self.platform_id}|{self.client.group_id}"
        if route not in self.bridge.config.get("allowed_groups", []):
            return None
        text = item["text"]
        mentioned = item["mentioned"]
        prefix = self.bridge.config.get("trigger_prefix", "/warm ")
        if prefix and text.startswith(prefix):
            mentioned = True
            text = text[len(prefix):]
        if not text.strip():
            return None
        # Preserve the old plugin's textual At conversion and default transport;
        # both participate in the existing message fingerprint.
        return Incoming(
            platform_id=self.platform_id, bot_id=self.client.bot_id,
            group_id=self.client.group_id, sender_id=item["sender_id"],
            sender_name=item["sender_name"], message_id=item["message_id"],
            sent_at=datetime.fromtimestamp(item["timestamp"], timezone.utc).isoformat(),
            text=text, mentioned=mentioned,
            mentioned_ids=[self.client.bot_id] if item["mentioned"] else [],
            message_type=item.get("message_type", "text"),
            quoted_message=item.get("quoted_message"),
        )

    async def handle(self, payload):
        message = self.message(payload)
        if message is None or self.observe_only or message.key in self.inflight:
            return
        self.scope = message.scope
        self.inflight.add(message.key)
        try:
            result = await self.bridge.receive(message)
            if result.get("status") == "prepared":
                await self.deliver(result)
        finally:
            self.inflight.discard(message.key)

    async def deliver(self, delivery):
        replies = delivery["reply_parts"]
        if (not isinstance(replies, list) or not 1 <= len(replies) <= 3
                or any(not isinstance(text, str) or not text.strip() for text in replies)):
            raise ValueError("invalid reply parts")
        async with self.delivery_lock:
            key = delivery["id"]
            if not (await self.bridge.claim(key))["claimed"]:
                return
            started = None
            stage = "send"
            try:
                for index, text in enumerate(replies):
                    started = time.monotonic()
                    stage = "send"
                    await self.client.send(text)
                    stage = "ack"
                    await self.bridge.acknowledge(key, "sent", index,
                        elapsed_seconds=min(3600., time.monotonic() - started))
            except (Exception, asyncio.CancelledError) as error:
                reason = delivery_error_code(error, stage)
                logger.warning("WeChat delivery stopped: %s", reason)
                try:
                    await self.bridge.acknowledge(key, "unknown", reason_code=reason,
                        elapsed_seconds=min(3600., time.monotonic() - started) if started is not None else None)
                except Exception:
                    logger.warning("WeChat unknown acknowledgement failed")
                if isinstance(error, asyncio.CancelledError):
                    raise

    async def poll(self):
        if self.scope is None or self.observe_only:
            return
        kind = None if self.bridge.config.get("proactive", {}).get("enabled", False) else "computer_report"
        for delivery in pending_deliveries(self.bridge.path, self.scope, limit=3, kind=kind):
            await self.deliver(delivery)

    async def _poll_loop(self):
        while not self.stopped:
            await asyncio.sleep(10)
            try:
                await self.poll()
            except Exception as error:
                logger.warning("WeChat pending poll failed: %s", delivery_error_code(error, "poll"))

    def _finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("WeChat incoming message failed: %s", type(task.exception()).__name__)

    async def run(self):
        if not self.client.settings.group_id or not self.client.settings.bot_id:
            raise NotReady("RUN_CHECK_WECHAT_CONFIGURE_FIRST")
        await self.client.bind()
        route = f"{self.platform_id}|{self.client.group_id}"
        if route not in self.bridge.config.get("allowed_groups", []):
            raise NotReady("GROUP_NOT_ALLOWED")
        scope = digest([self.platform_id, self.client.bot_id, self.client.group_id])[:32]
        # Restarted reports may use a previously observed, authenticated route.
        if scope in self.bridge.routes:
            self.scope = scope
        self.bridge.start()
        self.poll_task = asyncio.create_task(self._poll_loop())
        while not self.stopped:
            await self.client.check()
            try:
                async for payload in self.client.events():
                    await self.client.check()
                    task = asyncio.create_task(self.handle(payload))
                    self.tasks.add(task)
                    task.add_done_callback(self._finished)
                if not self.stopped:
                    raise httpx.ReadError("reader stream closed")
            except httpx.TransportError:
                if not self.stopped:
                    logger.warning("WeChat reader stream interrupted")
                    await asyncio.sleep(3)

    async def close(self):
        self.stopped = True
        tasks = [*self.tasks, *([self.poll_task] if self.poll_task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.poll_task = None
        await self.client.close()
