"""Loopback-only client for the WeChatAgent 0.3.9 reader/native contract.

Implemented from the published HTTP contract; no upstream Hook code is bundled.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import logging
from pathlib import Path
import re
import time
import tomllib
from urllib.parse import urlsplit

import httpx


SUPPORTED_VERSION = "4.1.10.27"
RECEIPT_CLOCK_SKEW_SECONDS = 10
logger = logging.getLogger(__name__)


class NotReady(RuntimeError):
    """An error code suitable for logs without tokens or message contents."""


def loopback_origin(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise NotReady("LOOPBACK_ORIGIN_REQUIRED")
    return value.rstrip("/")


def identifier(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 160
        and value.strip().lower() not in {"unknown", "none", "null", "0"}
        and not any(c in value for c in "|\r\n")
    )


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


@dataclass
class Settings:
    reader_url: str
    reader_token: str
    native_url: str
    native_token: str
    group_name: str
    bot_nickname: str
    group_id: str = ""
    bot_id: str = ""

    @classmethod
    def load(cls, filename):
        file = Path(filename).resolve()
        data = tomllib.loads(file.read_text(encoding="utf-8-sig"))["wechatagent"]
        if not data.get("project_dir"):
            raise NotReady("SET_WECHATAGENT_DIRECTORY")
        if not data.get("group_name") or not data.get("bot_nickname"):
            raise NotReady("SET_GROUP_AND_NICKNAME")
        directory = Path(data["project_dir"])
        if not directory.is_absolute():
            directory = file.parent.parent / directory
        io_file = directory / data.get("io_config", "state/wechat-io-config.json")
        token_file = directory / data.get("native_token", "state/hook/native-token.txt")
        if not io_file.is_file() or not token_file.is_file():
            raise NotReady("WECHATAGENT_RUNTIME_FILES_MISSING")
        io = read_json(io_file)
        token = token_file.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise NotReady("NATIVE_TOKEN_INVALID")
        if not isinstance(io.get("reader_token"), str) or len(io["reader_token"]) < 32:
            raise NotReady("READER_TOKEN_INVALID")
        return cls(
            loopback_origin(io["reader_base_url"]),
            io["reader_token"],
            loopback_origin(data.get("native_url", "http://127.0.0.1:30001")),
            token,
            data["group_name"],
            data["bot_nickname"],
            data.get("group_id", ""),
            data.get("bot_id", ""),
        )


class NativeClient:
    def __init__(self, settings, *, transport=None, receipt_timeout=18):
        self.settings = settings
        self.http = httpx.AsyncClient(
            timeout=8, follow_redirects=False, trust_env=False, transport=transport
        )
        self.receipt_timeout = receipt_timeout
        self.account = ""
        self.bot_id = ""
        self.group_id = ""
        self.send_lock = asyncio.Lock()

    async def close(self):
        await self.http.aclose()

    def reader_headers(self):
        return {"Authorization": "Bearer " + self.settings.reader_token}

    async def reader(self, route, **params):
        response = await self.http.get(
            self.settings.reader_url + route,
            params=params,
            headers=self.reader_headers(),
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("success") is False:
            raise NotReady("READER_NOT_READY")
        return result

    async def native(self, route, body=None):
        headers = {"Authorization": "Bearer " + self.settings.native_token}
        if self.account:
            headers["X-WeChatAgent-Account"] = self.account
        response = await self.http.request(
            "GET" if body is None else "POST",
            self.settings.native_url + route,
            headers=headers,
            **({"json": body} if body is not None else {}),
        )
        response.raise_for_status()
        return response.json()

    async def check(self):
        try:
            reader = await self.reader("/api/v1/reader/status")
        except httpx.RequestError:
            raise NotReady("READER_HTTP_UNAVAILABLE") from None
        account, own = reader.get("accountDirectory"), reader.get("ownWxid")
        if (
            reader.get("backend") != "weflow-cli-nt"
            or not identifier(account)
            or not identifier(own)
        ):
            raise NotReady("READER_ACCOUNT_INVALID")
        if self.settings.bot_id and own != self.settings.bot_id:
            raise NotReady("READER_ACCOUNT_CHANGED")
        if self.bot_id and own != self.bot_id:
            raise NotReady("READER_ACCOUNT_CHANGED")
        try:
            native = await self.native("/WeChatAgent/health")
        except httpx.RequestError:
            raise NotReady("NATIVE_HTTP_UNAVAILABLE") from None
        if (
            not isinstance(native, dict)
            or native.get("integration") != "WeChatAgent-1"
            or native.get("backend") != "wechat-hook"
        ):
            raise NotReady("REVIEWED_HOOK_BUILD_REQUIRED")
        if native.get("version") != SUPPORTED_VERSION:
            raise NotReady("HOOK_VERSION_MISMATCH")
        if native.get("startupState", "ready") != "ready":
            raise NotReady("HOOK_INITIALIZATION_PENDING")
        accounts = native.get("databaseAccounts")
        if (
            not isinstance(accounts, list)
            or len(accounts) != 1
            or not isinstance(accounts[0], str)
            or accounts[0].casefold() != account.casefold()
        ):
            raise NotReady("HOOK_ACCOUNT_MISMATCH")
        if self.account and self.account.casefold() != account.casefold():
            raise NotReady("HOOK_ACCOUNT_CHANGED")
        self.account, self.bot_id = account, own
        return reader

    async def bind(self):
        await self.check()
        rows = (await self.reader("/api/v1/sessions", limit=500)).get("sessions")
        if not isinstance(rows, list):
            raise NotReady("SESSIONS_INVALID")
        matches = {
            row["username"]
            for row in rows
            if isinstance(row, dict)
            and identifier(row.get("username"))
            and row["username"].endswith("@chatroom")
            and (
                row["username"] == self.settings.group_id
                if self.settings.group_id
                else row.get("displayName") == self.settings.group_name
            )
        }
        if len(matches) != 1:
            raise NotReady("GROUP_MAPPING_AMBIGUOUS_OR_MISSING")
        self.group_id = matches.pop()
        return self.group_id

    async def rows(self):
        result = await self.reader("/api/v1/messages", talker=self.group_id, limit=100)
        if not isinstance(result.get("messages"), list):
            raise NotReady("RECEIPTS_INVALID")
        return result["messages"]

    async def send(self, text):
        async with self.send_lock:
            await self._send(text)

    async def _send(self, text):
        if not self.group_id or not text.strip() or len(text) > 8000:
            raise NotReady("INVALID_SEND")
        await self.check()
        before = {str(row.get("localId")) for row in await self.rows()}
        started = time.time()
        # Check the account again immediately before the sole submission.
        await self.check()
        response = await self.native(
            "/SendTextMsg", {"wxidorgid": self.group_id, "msg": text}
        )
        if (
            not isinstance(response, dict)
            or type(response.get("ret")) is not int
            or response["ret"] != 0
        ):
            raise NotReady("NATIVE_SEND_REJECTED")
        deadline = time.monotonic() + self.receipt_timeout
        while True:
            await self.check()
            for row in await self.rows():
                server_id = str(row.get("serverId", ""))
                sent_at = row.get("createTime")
                if (
                    type(row.get("isSend")) in (bool, int)
                    and row["isSend"] == 1
                    and row.get("localType") == 1
                    and re.fullmatch(r"[1-9][0-9]*", str(row.get("localId", "")))
                    and str(row.get("localId")) not in before
                    and type(sent_at) in (int, float)
                    # WeChat's integer database timestamp can lag this process
                    # by several seconds. New local ID + exact body + server ID
                    # remain mandatory; never accept an old pre-send row.
                    and int(started) - RECEIPT_CLOCK_SKEW_SECONDS <= sent_at
                    <= time.time() + RECEIPT_CLOCK_SKEW_SECONDS
                    and row.get("fullText", row.get("content")) == text
                    and re.fullmatch(r"[0-9]{1,32}", server_id)
                    and int(server_id) > 0
                ):
                    if sent_at < int(started):
                        logger.info("MYATRI receipt accepted with clock_backstep_seconds=%d",
                                    int(started) - sent_at)
                    return
            if time.monotonic() >= deadline:
                raise NotReady("SEND_RECEIPT_UNKNOWN")
            await asyncio.sleep(0.4)

    async def events(self):
        async with self.http.stream(
            "GET",
            self.settings.reader_url + "/api/v1/push/messages",
            headers=self.reader_headers(),
            timeout=40,
        ) as response:
            response.raise_for_status()
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise NotReady("READER_STREAM_INVALID")
            async for line in response.aiter_lines():
                if len(line) > 65536:
                    raise NotReady("READER_EVENT_TOO_LARGE")
                if line.startswith("data:"):
                    payload = json.loads(line[5:])
                    if isinstance(payload, dict):
                        yield payload


def incoming(payload, client, started_at):
    """Reject history, self echoes, missing IDs and all other conversations."""
    stamp = payload.get("timestamp")
    sender = payload.get("talkerId")
    text = payload.get("content")
    quote = payload.get("quoteReply")
    quoted = (type(payload.get("type")) is int and payload["type"] in (49, (57 << 32) | 49)
              and isinstance(quote, dict) and quote.get("version") == 1 and quote.get("truncated") is False
              and isinstance(quote.get("reference"), dict) and isinstance(quote.get("reply"), str)
              and quote["reply"] == text)
    if (
        payload.get("event") != "message.new"
        or type(payload.get("type")) is not int
        or (payload["type"] != 1 and not quoted)
        or payload.get("sessionType") != "group"
        or payload.get("sessionId") != client.group_id
        or payload.get("isSend", False) is not False
        or not identifier(sender)
        or sender == client.bot_id
        or not identifier(payload.get("rawid"))
        or type(stamp) not in (int, float)
        or not started_at <= stamp <= time.time() + 300
        or not isinstance(text, str)
        or not text.strip()
        # The reference reader truncates content to 4000 characters. Reject
        # that boundary instead of saving possibly partial text as complete.
        or len(text) >= 4000
    ):
        return None
    # This is an explicit textual wake token, not an authenticated WeChat At ID.
    nick = re.escape(client.settings.bot_nickname)
    mentioned = bool(re.search(r"@" + nick + r"(?=\u2005|\s|$)", text))
    result = {
        "sender_id": sender,
        "sender_name": str(payload.get("sourceName") or sender)[:160],
        "message_id": payload["rawid"],
        "text": text,
        "timestamp": stamp,
        "mentioned": mentioned,
    }
    if quoted:
        ref = quote["reference"]
        if (ref.get("conversationId") == client.group_id and ref.get("kind") == "1"
                and isinstance(ref.get("text"), str) and len(ref["text"]) <= 1600
                and identifier(ref.get("senderId")) and ref.get("truncated") is False
                and ref.get("unavailable") is False):
            result["quoted_message"] = {"sender_id": ref["senderId"], "sender_name": str(ref.get("senderName", ""))[:160],
                "server_message_id": str(ref.get("messageId", ""))[:160], "sent_at": str(ref.get("time", ""))[:80],
                "text": ref["text"], "complete": True}
        result["message_type"] = "quote"
    return result
