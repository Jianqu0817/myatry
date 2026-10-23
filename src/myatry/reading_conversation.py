"""Bridge-local adapter for the frozen myatri 0.2.7 conversation protocol.

respond mirrors vendor 0.2.7, with bounded model projections, read coverage and
lightweight audits. Keep retry/idempotency/budget/checkpoint semantics aligned
when updating that dependency. No changes to site-packages or the core checkout.
"""
from copy import deepcopy
import inspect
import json
import logging
import time

import jsonschema

from myatri.contracts import ModelRequest
from myatri.conversation.serialization import compact_json
from myatri.conversation.engine import Conversation, TOOLS
from myatri.providers.gateway import ProviderError, assistant_message, validate_protocol
from .tool_reading import (READING_RULES, FIELDS, cold_page, guard_reading_claim, read_audit,
                           read_page, recall_directory)
from .bubbles import prepare_bubbles

logger = logging.getLogger(__name__)


READING_TOOLS = deepcopy(TOOLS)
READING_TOOLS[0]["function"]["description"] = "搜索记忆目录；摘录不是已读原文，用read_record读取精确版本"
reader = READING_TOOLS[1]["function"]
reader["description"] = "分页读取精确ID@revision；按read_progress.first_unread_offset补缺页，末页不代表读完"
reader["parameters"]["properties"]["ref"]["maxLength"] = 256
reader["parameters"]["properties"]["field"] = {"type": "string", "enum": list(FIELDS)}


class ReadingConversation(Conversation):
    @classmethod
    def adapt(cls, conversation, chat_options=None):
        # Transfer the already constructed hooks, context, lock and shared
        # budgets without reinitializing runtime state or changing its class.
        adapted = cls.__new__(cls)
        adapted.__dict__.update(conversation.__dict__)
        options = chat_options or {}
        adapted.thinking = options.get("thinking", False)
        adapted.reasoning_effort = options.get("reasoning_effort", "high")
        adapted.thinking_max_tokens = options.get("thinking_max_tokens", 8192)
        adapted.thinking_timeout = options.get("thinking_timeout", 120)
        if (type(adapted.thinking) is not bool
                or not isinstance(adapted.reasoning_effort, str)
                or adapted.reasoning_effort not in {"low", "high", "max"}
                or type(adapted.thinking_max_tokens) is not int
                or not 1024 <= adapted.thinking_max_tokens <= 393216
                or type(adapted.thinking_timeout) not in {int, float}
                or not 0 < adapted.thinking_timeout <= 600):
            raise ValueError("invalid conversation thinking settings")
        if adapted.thinking:
            adapted.context.budget.output_reserve = max(
                adapted.context.budget.output_reserve, adapted.thinking_max_tokens)
        return adapted

    async def respond(self, text, *, message_id=None, metadata=None):
        started = time.perf_counter()
        async with self._lock:
            acquired = time.perf_counter()
            timings = {"lock_wait_seconds": acquired-started, "model_seconds": 0.,
                       "guard_seconds": 0., "tool_seconds": 0.}
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError("message metadata must be a dict")
            if metadata and set(metadata) & {"role", "content", "message_id"}:
                raise ValueError("message metadata cannot override the conversation identity")
            raw = self.store.append_message("user", text, message_id=message_id, **(metadata or {}))
            try:
                answer = self.store.get_record("answer_" + raw.record_id)
            except KeyError:
                pass
            else:
                if self.on_answer:
                    self.on_answer(answer)
                return answer
            # Queue ingestion before API wait; user text remains safe if the provider fails.
            if self.on_message:
                self.on_message(raw)
            # Build an immutable per-turn view; shared group handler state must
            # never carry an owner's authority into another member's request.
            handlers = dict(self.tool_handlers)
            if getattr(self, "turn_tool_factory", None):
                handlers.update(self.turn_tool_factory(raw))
            tools = [*READING_TOOLS, *[entry[0] for entry in handlers.values()]] if self.enable_tools else []
            completed = sum(1 for r in self.store.list_records("raw_message")
                            if r.payload.get("role") == "assistant" and r.payload.get("reply_to"))
            checkpoint = (completed + 1) % self.context.trace_every_turns == 0
            timings["input_seconds"] = time.perf_counter()-acquired
            context_started = time.perf_counter()
            saved = self.protocol_store.get_protocol(raw.record_id) if self.protocol_store else None
            if saved and saved["status"] == "context_reset":
                raise ProviderError("turn belongs to a cleared context; use a new message ID")
            delegated = ('computer_control' in handlers and 'instructions' in
                         handlers['computer_control'][0]['function']['parameters'].get('properties', {}))
            from .computer_control import command_body, explicit_computer_query, ComputerTaskPending
            source_text = raw.payload.get('channel_source', {}).get('text', '')
            command = command_body(source_text)
            delegation_required = delegated and ((command is not None and command.startswith('任务 '))
                                                 or explicit_computer_query(source_text))
            if self.enable_tools and "computer_control" in handlers and not delegated:
                # /pc is already an exact, owner-bound host command. Dispatch it
                # once without asking the chat model whether it has this ability.
                # The independent Harness agent still plans and executes the goal.
                from .computer_control import command_receipt
                result = handlers["computer_control"][1]()
                if inspect.isawaitable(result):
                    result = await result
                body = command_receipt(result)
                answer = self.store.append_message("assistant", body,
                    message_id="answer_" + raw.record_id, reply_to=raw.ref,
                    provider="host", model="computer-command", reply_parts=[body],
                    computer_command_receipt={k: result[k] for k in
                        ("task_id", "state", "status", "error") if k in result})
                if self.protocol_store:
                    self.protocol_store.save_protocol(raw.record_id, {
                        "status": "completed", "steps": 0, "reply_ref": answer.ref,
                        "sent_at": raw.created_at, "replied_at": answer.created_at,
                        "computer_command": True, "full_checkpoint": False,
                        "tool_audit": [], "reading_guard": None,
                        "latency": {"total_seconds": round(time.perf_counter()-started, 4),
                                    "model_seconds": 0.0},
                    })
                if self.on_answer:
                    self.on_answer(answer)
                return answer
            if saved and saved["status"] == "active":
                messages, snapshot, start_step = saved["messages"], saved["snapshot"], saved["steps"]
                turn_stats = deepcopy(saved.get("context_stats", {}))
                validate_protocol(messages)
            else:
                messages, snapshot = await self.context.abuild_context(text, tools=tools, persist_activation=checkpoint)
                turn_stats = deepcopy(self.context.explain_context().get("context_stats", {}))
                start_step = 0
            # Resume the original call, never a separate report-generation chat.
            # Re-dispatch is idempotent: the source message fixes the task ID and
            # an existing task returns its persisted state without repeating effects.
            waiting = saved.get('computer_wait') if saved else None
            if waiting:
                if 'computer_control' not in handlers:
                    result = {'error': 'computer_owner_required'}
                else:
                    result = await handlers['computer_control'][1](**waiting['arguments'])
                for item in messages:
                    if item.get('role') == 'tool' and item.get('tool_call_id') == waiting['call_id']:
                        item['content'] = compact_json(result)
                        break
                else:
                    raise ProviderError('computer continuation missing its tool call')
                pending = result.get('task_id') and result.get('state') in {'queued', 'running', 'pausing', 'cancelling'}
                self.protocol_store.save_protocol(raw.record_id, {'status': 'active', 'snapshot': snapshot,
                    'steps': start_step, 'messages': messages, 'context_stats': turn_stats,
                    **({'computer_wait': waiting} if pending else {})})
                if pending:
                    raise ComputerTaskPending(result['task_id'])
            timings["context_seconds"] = time.perf_counter()-context_started
            preparation = self.context.explain_context().get("preparation_timings", {})
            rules = READING_RULES
            if "computer_control" in handlers:
                from .computer_control import RULES
                rules = rules + "\n" + RULES
            if "web_search" in self.tool_handlers:
                from .web_tools import WEB_SEARCH_RULES
                # The tool description covers "how"; this rule covers "when".
                rules = rules + "\n" + WEB_SEARCH_RULES
            if not any(m.get("role") == "system" and m.get("content") == rules for m in messages):
                messages.insert(0, {"role": "system", "content": rules})
            delegation_calls = {call['id'] for m in messages for call in m.get('tool_calls', [])
                                if call.get('function', {}).get('name') == 'computer_control'}
            task_submitted = False
            delegation_attempted = False
            for message in messages:
                if message.get('role') == 'tool' and message.get('tool_call_id') in delegation_calls:
                    try:
                        result = json.loads(message['content'])
                        task_submitted = task_submitted or bool(result.get('task_id'))
                        delegation_attempted = True
                    except (ValueError, AttributeError):
                        pass
            for step in range(start_step, self.max_steps):
                reply_policy = self.context.reply_policy
                output_tokens = self.context.budget.output_reserve
                if self.thinking:
                    output_tokens = self.thinking_max_tokens
                elif reply_policy.maximum:
                    output_tokens = min(output_tokens, reply_policy.max_tokens)
                request = ModelRequest(messages=messages, tools=tools, role="foreground", purpose="conversation",
                                       budget_tokens=self.context.budget.limit, max_tokens=output_tokens,
                                       thinking=self.thinking, reasoning_effort=self.reasoning_effort,
                                       timeout=self.thinking_timeout if self.thinking else 45,
                                       estimated_input_tokens=self.context.budget.estimate(messages, tools))
                self.context.budget.require(messages, tools)
                model_started = time.perf_counter()
                response = await self.gateway.generate(request)
                timings["model_seconds"] += time.perf_counter()-model_started
                self.context.budget.calibrate(messages, response.usage, tools)
                if self.protocol_store and checkpoint:
                    self.protocol_store.set_meta("context_token_anchor", self.context.budget.anchor)
                if delegation_required and not delegation_attempted and not task_submitted and not response.tool_calls:
                    # One explicit correction, preserving the main model's role
                    # as delegator instead of silently substituting host prose.
                    if step > start_step:
                        raise ProviderError('computer delegation missing; task was not submitted')
                    messages.append(assistant_message(response))
                    messages.append({'role': 'system', 'content':
                        '当前已核验用户明确提交电脑任务。请生成instructions并调用computer_control委派Harness，'
                        '不能只回复做不到或声称已接单。工具返回实际执行报告后，再回答用户。'})
                    continue
                if not response.tool_calls:
                    if self.reply_guard is not None:
                        guard_started = time.perf_counter()
                        checked = await self.reply_guard(response.content, raw, messages)
                        timings["guard_seconds"] += time.perf_counter()-guard_started
                        response = response.model_copy(update={"content": checked})
                    guarded, reading_guard = guard_reading_claim(response.content, read_audit(messages))
                    body, reply_parts = prepare_bubbles(guarded, reply_policy)
                    response = response.model_copy(update={"content": body})
                    if not response.content:
                        raise ProviderError("empty reply after metadata removal; raw user message preserved")
                messages.append(assistant_message(response))
                if not response.tool_calls:
                    validate_protocol(messages)
                    answer = self.store.append_message("assistant", response.content,
                        message_id="answer_" + raw.record_id, reply_to=raw.ref,
                        provider=response.provider, model=response.model, context_snapshot=snapshot,
                        reading_guard=reading_guard, reply_parts=reply_parts,
                        **({"quality_review": self.reply_guard.last_check} if self.reply_guard is not None
                           and hasattr(self.reply_guard, "last_check") else {}))
                    timings["total_seconds"] = time.perf_counter()-started
                    timings["local_other_seconds"] = max(0., timings["total_seconds"] - sum(
                        value for key, value in timings.items() if key != "total_seconds"))
                    latency = {key: round(value, 4) for key, value in timings.items()}
                    latency["context_preparation"] = preparation
                    logger.info("MYATRI reply generation latency: %s", compact_json(latency))
                    if self.protocol_store:
                        protocol = {"status": "completed", "snapshot": snapshot,
                            "steps": step+1, "reply_ref": answer.ref, "sent_at": raw.created_at,
                            "replied_at": answer.created_at, "full_checkpoint": checkpoint,
                            "tool_audit": read_audit(messages), "reading_guard": reading_guard,
                            "context_stats": turn_stats, "latency": latency}
                        if self.thinking:
                            # Provider protocol data, never character memory. Needed
                            # when a later tools-enabled request replays this answer.
                            protocol["reasoning_content"] = response.provider_fields.get("reasoning_content", "")
                        if checkpoint or delegation_calls or task_submitted:
                            protocol["messages"] = messages
                            protocol["context"] = self.context.explain_context()
                        self.protocol_store.save_protocol(raw.record_id, protocol)
                    if checkpoint:
                        self.store.flush_projection()
                    if self.on_answer:
                        self.on_answer(answer)
                    return answer
                tools_started = time.perf_counter()
                computer_wait = None
                for call in response.tool_calls:
                    try:
                        name = call["function"]["name"]
                        definition = next(t["function"] for t in tools if t["function"]["name"] == name)
                        args = json.loads(call["function"]["arguments"])
                        jsonschema.validate(args, definition["parameters"])
                        if name == "recall":
                            bundle = self.recall.recall(args["query"], snapshot=snapshot, budget=1800)
                            result = recall_directory(bundle, self.store)
                        elif name in handlers:
                            if name == 'computer_control' and delegated:
                                if len(response.tool_calls) != 1:
                                    raise ValueError('computer delegation must be a single tool call')
                                if step+1 >= self.max_steps or not self.protocol_store:
                                    raise ValueError('computer delegation requires a resumable final step')
                                computer_wait = {'call_id': call['id'], 'arguments': args}
                                # Save the call BEFORE submitting an execution task.
                                # A crash between submission and dispatch completion
                                # can resume this exact call without another LLM plan.
                                if self.protocol_store:
                                    self.protocol_store.save_protocol(raw.record_id, {
                                        'status': 'active', 'snapshot': snapshot, 'steps': step+1,
                                        'messages': [*messages, {'role': 'tool', 'tool_call_id': call['id'],
                                            'content': compact_json({'state': 'awaiting_dispatch'})}],
                                        'context_stats': turn_stats, 'computer_wait': computer_wait})
                            result = handlers[name][1](**args)
                            # A blocking backend (e.g. an out-of-process search) must
                            # not stall the loop shared by every group, so async
                            # handlers are awaited and run in their own worker thread.
                            if inspect.isawaitable(result):
                                result = await result
                            if name == 'computer_control':
                                delegation_attempted = True
                                task_submitted = bool(result.get('task_id'))
                            if name == "read_group_message":
                                result = cold_page(result, args["message_id"], read_audit(messages))
                        else:
                            result = read_page(self.store, args["ref"], snapshot, read_audit(messages),
                                offset=args.get("offset", 0), limit=args.get("limit", 800), field=args.get("field"))
                        # Bounded result, without truncating JSON/protocol fields.
                        encoded = compact_json(result)
                        if len(encoded.encode("utf-8")) > 6000:
                            result = {"error": "record exceeds tool result budget", "ref": args.get("ref"), "suggestion": "use recall with narrower query"}
                    except (ValueError, KeyError, StopIteration, jsonschema.ValidationError) as exc:
                        result = {"error": type(exc).__name__, "message": "tool arguments or source invalid"}
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": compact_json(result)})
                timings["tool_seconds"] += time.perf_counter()-tools_started
                if self.protocol_store:
                    pending = (computer_wait and result.get('task_id') and
                               result.get('state') in {'queued', 'running', 'pausing', 'cancelling'})
                    self.protocol_store.save_protocol(raw.record_id, {"status": "active", "snapshot": snapshot,
                        "steps": step+1, "messages": messages, "tool_audit": read_audit(messages),
                        "context_stats": turn_stats,
                        **({'computer_wait': computer_wait} if pending else {})})
                    if pending:
                        raise ComputerTaskPending(result['task_id'])
            raise ProviderError("tool step limit reached; raw user message preserved, no fictitious reply committed")
