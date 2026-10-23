"""Explicit offline/live adapters. No import or default construction performs network IO."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
import jsonschema

from myatri.contracts import ModelRequest, ModelResponse


class ProviderError(RuntimeError):
    pass


class BudgetExceeded(ProviderError):
    def __init__(self, message, *, reason="request_size"):
        super().__init__(message)
        self.reason = reason


def validate_protocol(messages: list[dict], *, complete: bool = True) -> None:
    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages:
        role = message.get("role")
        if pending and role != "tool":
            raise ProviderError("unfinished tool roundtrip")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ProviderError("orphan or duplicate tool result")
            pending.remove(call_id)
        for call in message.get("tool_calls") or []:
            if role != "assistant" or not call.get("id") or call["id"] in seen:
                raise ProviderError("invalid or duplicate tool call")
            seen.add(call["id"])
            pending.add(call["id"])
    if complete and pending:
        raise ProviderError("unfinished tool roundtrip")


def assistant_message(response: ModelResponse) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    if response.tool_calls:
        message["tool_calls"] = response.tool_calls
    # Protocol field remains in the transient request, never in a character record.
    if "reasoning_content" in response.provider_fields:
        message["reasoning_content"] = response.provider_fields["reasoning_content"]
    return message


class FixtureProvider:
    """Deterministic protocol fixtures, never a claim of semantic model quality."""
    name = "fixture"

    def __init__(self, responses: list[dict | ModelResponse] | None = None, delay: float = 0):
        self.responses = deque(responses or [])
        self.delay = delay
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request.model_copy(deep=True))
        await asyncio.sleep(self.delay)
        if self.responses:
            value = self.responses.popleft()
            return value if isinstance(value, ModelResponse) else ModelResponse(
                provider="fixture", model="offline-fixture-v1", **value)
        if request.role != "foreground":
            value = {"decision": "defer", "reason": "离线 fixture 不生成语义判断"}
            properties = (request.output_schema or {}).get("properties", {})
            for key in (request.output_schema or {}).get("required", []):
                if key not in value:
                    field = properties.get(key, {})
                    value[key] = [] if field.get("type") == "array" else False if field.get("type") == "boolean" else ""
            return ModelResponse(content=json.dumps(value, ensure_ascii=False),
                                 provider="fixture", model="offline-fixture-v1")
        text = "（离线演示）我已保存这轮对话。可以用 recall 检查来源、reflect 整理已有材料。"
        return ModelResponse(content=text, provider="fixture", model="offline-fixture-v1",
                             usage={"prompt_tokens": sum(len(str(m)) for m in request.messages), "completion_tokens": len(text)})


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, *, model: str, api_key: str | None = None,
                 endpoint: str = "https://api.deepseek.com", allow_paid: bool = False,
                 capabilities_verified: bool = False, client: httpx.AsyncClient | None = None,
                 capability_probe: bool = False):
        self.model, self.endpoint = model, endpoint.rstrip("/")
        self.api_key = api_key  # Resolved only at request time, never on offline startup.
        self.allow_paid = allow_paid
        self.capabilities_verified = capabilities_verified
        self.capability_probe = capability_probe
        self.client = client

    @staticmethod
    def validate_endpoint(endpoint):
        url = urlsplit(endpoint)
        if (url.scheme != "https" or url.hostname != "api.deepseek.com" or
                url.port not in {None, 443} or url.username or url.password or
                url.path.rstrip("/") not in {"", "/v1"} or url.query or url.fragment):
            raise ProviderError("DeepSeek credentials are restricted to the official HTTPS endpoint")

    def _key(self):
        self.validate_endpoint(self.endpoint)
        if self.api_key:
            return self.api_key
        from myatri.credentials import read_deepseek_key
        return os.getenv("DEEPSEEK_API_KEY") or read_deepseek_key() or ""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if not self.allow_paid:
            raise ProviderError("paid API disabled; explicit future authorization required")
        key = self._key()
        if not key:
            raise ProviderError("DEEPSEEK_API_KEY missing")
        if (request.tools or request.thinking) and not (self.capabilities_verified or self.capability_probe):
            raise ProviderError("tools/thinking require separately verified live capabilities")
        body: dict[str, Any] = {"model": self.model, "messages": request.messages,
                                "max_tokens": request.max_tokens,
                                "thinking": {"type": "enabled" if request.thinking else "disabled"}}
        if request.thinking:
            body["reasoning_effort"] = request.reasoning_effort
        if request.tools:
            body["tools"] = request.tools
        if request.output_schema:
            # JSON mode plus local schema validation, not an unsupported strict schema claim.
            body["response_format"] = {"type": "json_object"}
            # JSON mode constrains syntax, not an arbitrary schema. Send the schema
            # to the model as data, then validate locally before any proposal commit.
            body["messages"] = [{"role": "system", "content": "Output JSON matching this JSON Schema: " +
                json.dumps(request.output_schema, ensure_ascii=False)}, *request.messages]
        async def call(client):
            result = await client.post(f"{self.endpoint}/chat/completions", json=body,
                                       headers={"Authorization": f"Bearer {key}"}, timeout=request.timeout,
                                       follow_redirects=False)
            result.raise_for_status()
            data = result.json()
            choice = data["choices"][0]
            message = choice["message"]
            return ModelResponse(content=message.get("content") or "", tool_calls=message.get("tool_calls") or [],
                                 usage=data.get("usage", {}), finish_reason=choice["finish_reason"],
                                 provider="deepseek", model=data.get("model", self.model),
                                 provider_fields={k: message[k] for k in ("reasoning_content",) if k in message})
        if self.client:
            return await call(self.client)
        async with httpx.AsyncClient(trust_env=False) as client:
            return await call(client)


class Gateway:
    def __init__(self, provider, *, foreground_limit=2, background_limit=1,
                 daily_token_limit=200000, retries=2, retry_delay=0.1, usage_sink=None,
                 input_per_million=0.0, output_per_million=0.0,
                 budget_ledger=None, budget_scope=None, request_observer=None):
        self.provider = provider
        self.semaphores = {"foreground": asyncio.Semaphore(foreground_limit),
                           "background": asyncio.Semaphore(background_limit)}
        self.daily_token_limit = daily_token_limit
        self.budget_ledger = budget_ledger
        # Account-sharing callers may supply a stable, non-secret account scope.
        # Provider scope is conservative across all domains within one runtime DB.
        self.budget_scope = budget_scope or getattr(provider, "name", type(provider).__name__)
        self._local_budgets = {}
        self.retries, self.retry_delay = retries, retry_delay
        self.usage_sink = usage_sink
        self.request_observer = request_observer
        self.input_price, self.output_price = input_per_million, output_per_million

    @staticmethod
    def _utc_day(now=None):
        return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).date().isoformat()

    def _local_budget(self, day=None):
        return self._local_budgets.setdefault(day or self._utc_day(), {"used_tokens": 0, "reserved_tokens": 0})

    @property
    def used_tokens(self):
        if self.budget_ledger is not None:
            return self.budget_ledger.budget_usage(self.budget_scope)["used_tokens"]
        return self._local_budget()["used_tokens"]

    @used_tokens.setter
    def used_tokens(self, value):
        if self.budget_ledger is not None:
            raise ValueError("shared usage is restored from the ledger, not a local counter")
        self._local_budget()["used_tokens"] = value

    @property
    def reserved_tokens(self):
        if self.budget_ledger is not None:
            return self.budget_ledger.budget_usage(self.budget_scope)["reserved_tokens"]
        return self._local_budget()["reserved_tokens"]

    def _reserve(self, estimated, request):
        now = time.time()
        day = self._utc_day(now)
        if self.budget_ledger is not None:
            reservation_id = self.budget_ledger.reserve_budget(
                self.budget_scope, estimated, self.daily_token_limit,
                ttl_seconds=min(3600, max(60, request.timeout + 30)), now=now)
            if reservation_id is None:
                raise BudgetExceeded("daily budget exhausted; defer background work", reason="daily_limit")
        else:
            budget = self._local_budget(day)
            if budget["used_tokens"] + budget["reserved_tokens"] + estimated > self.daily_token_limit:
                raise BudgetExceeded("daily budget exhausted; defer background work", reason="daily_limit")
            budget["reserved_tokens"] += estimated
            reservation_id = None
        return reservation_id, day, estimated

    def _settle(self, reservation, charged, *, unknown=False):
        reservation_id, day, estimated = reservation
        if self.budget_ledger is not None:
            self.budget_ledger.settle_budget(reservation_id, charged,
                                            status="unknown" if unknown else "settled")
        else:
            budget = self._local_budget(day)
            budget["reserved_tokens"] -= estimated
            budget["used_tokens"] += charged

    @staticmethod
    def _charged_tokens(response, estimated):
        def count(value):
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
        return (count(response.usage.get("total_tokens")) or
                count(response.usage.get("prompt_tokens")) + count(response.usage.get("completion_tokens")) or
                estimated)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        validate_protocol(request.messages)
        estimated = len(json.dumps(request.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")) + request.max_tokens
        if request.role == "foreground" and request.estimated_input_tokens is not None:
            estimated = request.estimated_input_tokens + request.max_tokens
        if estimated > request.budget_tokens:
            raise BudgetExceeded("full request including tools/protocol exceeds token budget")
        # User-facing context preparation must not queue behind a long A5/A8
        # job. Business roles remain distinct in usage/prompt accounting.
        lane = "foreground" if request.role in {"foreground", "activation", "compaction"} else "background"
        async with self.semaphores[lane]:
            for attempt in range(self.retries + 1):
                reservation = self._reserve(estimated, request)
                if self.request_observer is not None:
                    try:
                        self.request_observer(request, attempt)
                    except Exception:
                        # No exception text/traceback: filesystem names or request
                        # data may contain secrets. Capture is never required to chat.
                        logging.getLogger(__name__).warning("Context capture unavailable; chat continues.")
                started = time.monotonic()
                try:
                    try:
                        async with asyncio.timeout(request.timeout):
                            response = await self.provider.generate(request)
                    except BaseException as exc:
                        # A timeout, cancellation, crash or malformed response may
                        # occur after the provider bills us. Do not refund unknown IO.
                        self._settle(reservation, estimated, unknown=True)
                        failed = ModelResponse(provider=getattr(self.provider, "name", type(self.provider).__name__),
                                               model=getattr(self.provider, "model", "unknown"), content="")
                        self._log(request, failed, time.monotonic() - started,
                                  "cancelled_unknown" if isinstance(exc, asyncio.CancelledError) else "failed_unknown",
                                  estimated, reservation)
                        raise
                    charged = self._charged_tokens(response, estimated)
                    self._settle(reservation, charged)
                    self._log(request, response, time.monotonic() - started, "received", charged, reservation)
                    if response.finish_reason in {"length", "content_filter", "insufficient_system_resource"}:
                        raise ProviderError(f"incomplete response: {response.finish_reason}")
                    if not response.tool_calls and not response.content.strip():
                        raise ProviderError("empty model response")
                    if request.output_schema and not response.tool_calls:
                        try:
                            value = json.loads(response.content)
                            jsonschema.validate(value, request.output_schema)
                        except (ValueError, jsonschema.ValidationError) as exc:
                            raise ProviderError("invalid structured result; no changes submitted") from exc
                    return response
                except (httpx.TimeoutException, httpx.NetworkError, TimeoutError, httpx.HTTPStatusError) as exc:
                    retryable = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in {408, 429, 500, 502, 503, 504}
                    if not retryable or attempt == self.retries:
                        raise ProviderError(f"provider transport failed ({type(exc).__name__})") from None
                    await asyncio.sleep(self.retry_delay * 2**attempt)
            raise ProviderError("retry exhausted")

    def _log(self, request, response, elapsed, status, charged, reservation):
        if self.usage_sink:
            self.usage_sink({"trace_id": request.trace_id, "purpose": request.purpose,
                             "recorded_at": datetime.now(timezone.utc).isoformat(),
                             "thinking_enabled": request.thinking,
                             "reasoning_effort": request.reasoning_effort if request.thinking else None,
                             "finish_reason": response.finish_reason,
                             "request_max_tokens": request.max_tokens,
                             "request_budget_tokens": request.budget_tokens,
                             "estimated_input_tokens": request.estimated_input_tokens,
                             "reasoning_content_present": bool(response.provider_fields.get("reasoning_content")),
                             "day": reservation[1], "budget_reservation_id": reservation[0],
                             "budget_scope": self.budget_scope,
                             "role": request.role, "prompt_version": request.prompt_version,
                             "provider": response.provider, "model": response.model, "usage": response.usage,
                             "charged_tokens": charged,
                             "elapsed_seconds": elapsed, "status": status,
                             "estimated_cost": (response.usage.get("prompt_tokens", 0) * self.input_price +
                                                response.usage.get("completion_tokens", 0) * self.output_price) / 1e6})
