"""Opt-in, bounded local captures of requests prepared for a provider.

Captures are debugging data, not proof that the server received a request.
Never include provider credentials, headers or private reasoning fields.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid


SCHEMA_VERSION = "myatri-context-capture-v1"
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", re.IGNORECASE)
_CAPTURE_FILE = re.compile(r"[a-f0-9]{64}\.json\Z")
_MESSAGE_FIELDS = ("role", "content", "name", "tool_call_id")


def _message(message):
    result = {key: message[key] for key in _MESSAGE_FIELDS if key in message}
    if "tool_calls" in message:
        result["tool_calls"] = [
            {**{key: call[key] for key in ("id", "type") if key in call},
             "function": {key: call.get("function", {})[key] for key in ("name", "arguments")
                          if key in call.get("function", {})}}
            for call in message["tool_calls"] or []]
    return result


def _tool(tool):
    return {**{key: tool[key] for key in ("type",) if key in tool},
            "function": {key: tool.get("function", {})[key]
                         for key in ("name", "description", "parameters", "strict")
                         if key in tool.get("function", {})}}


def _redact(value):
    replacements = 0

    def visit(item):
        nonlocal replacements
        if isinstance(item, str):
            text, count = _TOKEN.subn("[REDACTED_TOKEN]", item)
            replacements += count
            return text
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            # Preserve insertion order and literal content. Do not parse JSON
            # strings containing channel envelopes or tool arguments.
            return {key: visit(child) for key, child in item.items()}
        return item

    result = visit(value)
    return result, replacements


class RequestCapture:
    def __init__(self, directory, *, provider_name="", model=None, keep=5, metadata_provider=None):
        if type(keep) is not int or not 1 <= keep <= 50:
            raise ValueError("invalid capture retention")
        self.directory = Path(directory)
        self.provider_name, self.model, self.keep = provider_name, model, keep
        self.metadata_provider = metadata_provider
        self._lock = threading.Lock()

    def __call__(self, request, attempt):
        if request.role != "foreground" or request.purpose not in {"conversation", "group_reply_guard"}:
            return
        messages = request.messages
        schema_added = self.provider_name == "deepseek" and request.output_schema is not None
        if schema_added:
            messages = [{"role": "system", "content": "Output JSON matching this JSON Schema: " +
                         json.dumps(request.output_schema, ensure_ascii=False)}, *messages]
        metadata, metadata_status = {}, "disabled"
        if self.metadata_provider is not None:
            try:
                supplied = self.metadata_provider()
                allowed = {key: supplied[key] for key in ("context_stats", "history_selection", "summary_omitted_items",
                           "active_topic", "budget_limit", "compact_at") if key in supplied}
                metadata = json.loads(json.dumps(allowed, ensure_ascii=False, allow_nan=False))
                metadata_status = "available"
            except Exception:
                metadata_status = "unavailable"
        snapshot = {"schema_version": SCHEMA_VERSION, "status": "prepared_for_provider",
            "trace_id": request.trace_id, "purpose": request.purpose, "role": request.role,
            "model": self.model, "captured_at": datetime.now(timezone.utc).isoformat(),
            "attempt": attempt, "provider_schema_added": schema_added,
            "limits": {"max_tokens": request.max_tokens, "budget_tokens": request.budget_tokens,
                       "estimated_input_tokens": request.estimated_input_tokens, "timeout_seconds": request.timeout,
                       "thinking_enabled": request.thinking,
                       "reasoning_effort": request.reasoning_effort if request.thinking else None},
            "messages": [_message(message) for message in messages],
            "tools": [_tool(tool) for tool in request.tools],
            "metadata": metadata, "metadata_status": metadata_status,
            "metadata_note": "Local selection diagnostics, not sent to the model and not model reasoning.",
            "excluded_fields": ["reasoning_content", "provider_fields", "credentials", "headers"],
            "note": "Prepared locally before provider call; delivery and usage must be checked in the usage ledger. "
                    "Message order and literal text are preserved except token-like secret redaction. "
                    "Private reasoning and non-allowlisted extensions are excluded."}
        snapshot, replacements = _redact(snapshot)
        snapshot["redaction"] = {"applied": bool(replacements), "replacements": replacements,
                                 "replacement": "[REDACTED_TOKEN]"}
        encoded = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
        name = hashlib.sha256(request.trace_id.encode("utf-8")).hexdigest() + ".json"
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory / name
            temporary = self.directory / ("." + name + "." + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(encoded, encoding="utf-8")
                temporary.replace(target)
                files = [path for path in self.directory.iterdir() if _CAPTURE_FILE.fullmatch(path.name)]
                files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
                # Keep this request even on filesystems with coarse mtime.
                retained = {target, *[path for path in files if path != target][:self.keep - 1]}
                for path in files:
                    if path not in retained:
                        path.unlink(missing_ok=True)
            finally:
                temporary.unlink(missing_ok=True)
