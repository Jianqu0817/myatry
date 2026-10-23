"""Direct, read-only DeepSeek Search API transport; no auxiliary agent/model.

Only the query and search options leave this process. Credentials are resolved
at dispatch time, and HTTP errors never expose response bodies or headers.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
import time
from typing import Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from .web_tools import ERROR_BACKEND, ERROR_OUTPUT, ERROR_TIMEOUT, WEB_SEARCH_TOOL, WebSearchError


SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
MAX_RESPONSE_BYTES = 1_000_000
# The API permits 128 characters; reserve 17 for " after:YYYY-MM-DD".
DIRECT_WEB_SEARCH_TOOL = deepcopy(WEB_SEARCH_TOOL)
DIRECT_WEB_SEARCH_TOOL["function"]["parameters"]["properties"]["query"]["maxLength"] = 111
DIRECT_WEB_SEARCH_TOOL["function"]["description"] = (
    WEB_SEARCH_TOOL["function"]["description"].replace("最多200字", "最多111字")
)


def _credential() -> str | None:
    from myatri.credentials import read_deepseek_key
    return os.getenv("DEEPSEEK_API_KEY") or read_deepseek_key()


class DeepSeekSearchBackend:
    """One HTTP request per uncached search, without retries or fallback.

    A fixed official endpoint and disabled redirects/proxies keep the existing
    credential boundary. Construction performs no IO, including credential IO.
    """

    def __init__(self, *, allow_network: bool = False,
                 key_resolver: Callable[[], str | None] = _credential,
                 transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.time):
        self.allow_network = allow_network
        self.key_resolver = key_resolver
        self.transport = transport
        self.clock = clock

    def __call__(self, query: str, *, max_results: int, recency_days: int | None,
                 timeout_seconds: float) -> str:
        if not self.allow_network:
            raise WebSearchError(ERROR_BACKEND)
        # after: is the documented date operator, not an ignored request field.
        if recency_days is not None:
            after = (datetime.fromtimestamp(self.clock(), ZoneInfo("Asia/Shanghai"))
                     - timedelta(days=recency_days)).date().isoformat()
            query = f"{query} after:{after}"
        if not 1 <= len(query) <= 128:
            raise WebSearchError(ERROR_OUTPUT)
        try:
            key = self.key_resolver()
            if not key:
                raise WebSearchError(ERROR_BACKEND)
            body = {"query": query, "count": max_results, "need_content": False}
            # Deliberately omit ori_question: never send the triggering chat,
            # persona, archives or local files to the search service.
            deadline = time.monotonic() + timeout_seconds
            with httpx.Client(trust_env=False, follow_redirects=False,
                              timeout=timeout_seconds, transport=self.transport) as client:
                with client.stream("POST", SEARCH_ENDPOINT, json=body,
                                   headers={"Authorization": f"Bearer {key}"}) as response:
                    if response.status_code in {408, 504}:
                        raise WebSearchError(ERROR_TIMEOUT)
                    if response.status_code != 200:
                        raise WebSearchError(ERROR_BACKEND)
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise WebSearchError(ERROR_OUTPUT)
                        if time.monotonic() > deadline:
                            raise WebSearchError(ERROR_TIMEOUT)
                        chunks.append(chunk)
            value = json.loads(b"".join(chunks))
        except WebSearchError:
            raise
        except (httpx.TimeoutException, TimeoutError):
            raise WebSearchError(ERROR_TIMEOUT) from None
        except (ValueError, UnicodeError):
            raise WebSearchError(ERROR_OUTPUT) from None
        except Exception:
            # No exception text/chaining: it may contain credentials or query.
            raise WebSearchError(ERROR_BACKEND) from None
        if not isinstance(value, dict) or not isinstance(value.get("results"), list):
            raise WebSearchError(ERROR_OUTPUT)
        rows = []
        for item in value["results"]:
            if (not isinstance(item, dict)
                    or not all(isinstance(item.get(k), str) for k in ("url", "title", "snippet"))
                    or item.get("date") is not None and not isinstance(item["date"], str)
                    or item.get("summary") is not None and not isinstance(item["summary"], str)):
                raise WebSearchError(ERROR_OUTPUT)
            try:
                url = urlsplit(item["url"])
                if (url.scheme not in {"http", "https"} or not url.hostname
                        or url.username or url.password):
                    raise ValueError
            except ValueError:
                raise WebSearchError(ERROR_OUTPUT) from None
            row = {"title": item["title"], "url": item["url"],
                   "snippet": item.get("summary") or item["snippet"]}
            rows.append(row)
        return json.dumps({"results": rows[:max_results]}, ensure_ascii=False)
