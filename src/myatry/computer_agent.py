"""One serial Harness worker. Host owns identity, effects, budgets and durable evidence."""

import asyncio
from copy import copy
import hashlib
import json
import os
from pathlib import Path
import time

import jsonschema

from myatri.contracts import ModelRequest, digest
from .computer_tasks import TaskStore
from .computer_tools import ComputerTools, TOOLS
from .computer_full import FullComputerTools, FULL_FINISH
from .dsh_client import INHERITED_ENV, CREATION_FLAGS


def model_messages(messages):
    """Translate Harness canonical blocks without discarding tool call/result identity."""
    result = []
    for message in messages:
        role = message["role"]
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if role == "tool":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": message["toolCallId"],
                    "content": "\n".join(
                        b["text"] for b in content if b.get("type") == "text"
                    ),
                }
            )
            continue
        text = []
        calls = []
        for block in content:
            if block["type"] == "text":
                text.append(block["text"])
            elif block["type"] == "tool-call":
                args = block["arguments"]
                calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": args
                            if isinstance(args, str)
                            else json.dumps(args, ensure_ascii=False),
                        },
                    }
                )
            elif block["type"] == "tool-result":
                value = block.get("content", [])
                rendered = (
                    value
                    if isinstance(value, str)
                    else "\n".join(b["text"] for b in value if b.get("type") == "text")
                )
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["toolCallId"],
                        "content": rendered,
                    }
                )
            else:
                raise ValueError("unsupported_model_block")
        if role != "tool":
            row = {"role": role, "content": "\n".join(text)}
            if calls:
                row["tool_calls"] = calls
            result.append(row)
    return result


class ComputerAgent:
    def __init__(self, computer, settings, gateway_for_scope, *, store=None):
        self.computer = computer
        self.settings = settings
        self.profile = settings.get("permission_profile", "bounded")
        if self.profile not in {"bounded", "full"}:
            raise ValueError("invalid_computer_permission_profile")
        self.enabled = computer.enabled and bool(settings.get("agent_enabled", False))
        self.gateway_for_scope = gateway_for_scope
        self.root = computer.project_root / "var/computer-control"
        self.store = store or (
            TaskStore(self.root / "tasks.sqlite3") if self.enabled else None
        )
        self.worker = None
        self.process = None
        self.stopping = False
        self.prompt = (
            Path(__file__)
            .with_name("prompts")
            .joinpath("executor_full.md" if self.profile == "full" else "executor.md")
            .read_text(encoding="utf-8")
        )
        self.prompt_hash = hashlib.sha256(self.prompt.encode()).hexdigest()
        self.desktop_lock = computer.lock
        if self.store:
            self.store.recover()

    def start(self):
        if self.enabled and self.worker is None:
            self.worker = asyncio.create_task(self.loop())

    def submit(self, source, task_id, *, instructions='', reply_mode='background'):
        if not self.enabled or not self.computer.authorized(source):
            return {"error": "computer_owner_required"}
        from .computer_control import command_body
        command = command_body(source["text"])
        if command is None and (not isinstance(instructions, str) or not instructions.strip()):
            return {'error': 'invalid_execution_instructions'}
        scope = digest([source["platform_id"], source["bot_id"], source["group_id"]])[
            :32
        ]
        if command is None or command.startswith("任务 "):
            if not isinstance(instructions, str) or len(instructions) > 6000:
                return {'error': 'invalid_execution_instructions'}
            goal = source['text'].strip() if command is None else command[3:].strip()
            if not goal or len(goal) > 6000:
                return {"error": "invalid_task_goal"}
            try:
                existing = self.store.get(task_id)
            except KeyError:
                existing = None
            if existing:
                if (existing["owner"], existing["scope"], existing["goal"]) != (
                    self.computer.owner_id,
                    scope,
                    goal,
                ):
                    return {"error": "task_identity_conflict"}
                return {
                    "task_id": task_id,
                    "state": existing["state"],
                    "private_results": "本机任务面板",
                }
            if self.store.pending_count() >= 10:
                return {"error": "computer_queue_full"}
            workspace = self.root / "workspaces" / task_id
            workspace.mkdir(parents=True, exist_ok=True)
            # Directory expansion is a host configuration change, never an LLM argument.
            contract = {
                "profile": self.profile,
                "roots": {"workspace": str(workspace)},
                "applications": "current-windows-account"
                if self.profile == "full"
                else ["notepad", "calculator"],
                "limits": {
                    "calls": 12,
                    "tokens": 150000,
                    "tools": 40,
                    "seconds_per_turn": 180,
                },
                "overwrite": self.profile == "full",
                "external_messages": "only-when-user-goal-explicit"
                if self.profile == "full"
                else False,
                "arbitrary_commands": self.profile == "full",
                "prompt": self.prompt,
                "goal": goal,
                "delegation": instructions[:6000],
                "report_to_chat": reply_mode != 'conversation',
                "reply_mode": reply_mode,
                "channel_source": {k: source[k] for k in (
                    'platform_id', 'bot_id', 'group_id', 'sender_id', 'message_id', 'text',
                    'sender_name', 'sent_at') if k in source},
            }
            if self.profile == "full":
                contract["limits"] = {
                    "calls": 100,
                    "tokens": 2000000,
                    "tools": 500,
                    "seconds_per_turn": 1800,
                }
            self.store.create(
                task_id, self.computer.owner_id, scope, goal, contract, self.prompt_hash
            )
            self.start()
            return {
                "task_id": task_id,
                "state": self.store.get(task_id)["state"],
                "private_results": "本机任务面板",
            }
        for word, operation in [
            ("状态", "status"),
            ("暂停", "pause"),
            ("取消", "cancel"),
            ("继续", "resume"),
        ]:
            if command.startswith(word + " "):
                parts = command[len(word) + 1 :].split(maxsplit=1)
                target = parts[0]
                if len(target) != 64 or any(
                    c not in "0123456789abcdef" for c in target
                ):
                    return {"error": "invalid_task_id"}
                try:
                    row = self.store.get(target)
                except KeyError:
                    return {"error": "task_not_found"}
                if row["owner"] != source["sender_id"] or row["scope"] != scope:
                    return {"error": "task_scope_denied"}
                if operation == "status":
                    return {
                        "task_id": target,
                        **{k: row[k] for k in ("state", "calls", "tokens")},
                    }
                # Steering may narrow the original goal, but cannot replace its persisted contract.
                result = self.store.control(
                    target,
                    operation,
                    command_id=task_id,
                    text=parts[1]
                    if len(parts) > 1
                    else "继续原目标；先重新观察当前状态。",
                )
                self.start()
                return result
        return {"error": "unsupported_computer_command"}

    def control(self, identifier, action, command_id):
        if not self.enabled:
            raise ValueError("computer_agent_disabled")
        return self.store.control(identifier, action, command_id=command_id)

    async def _send(self, packet):
        self.process.stdin.write(
            (json.dumps(packet, ensure_ascii=False) + "\n").encode()
        )
        await self.process.stdin.drain()

    async def _start_process(self):
        if self.process and self.process.returncode is None:
            return
        env = {
            k: os.environ[k]
            for k in INHERITED_ENV
            if k in os.environ and k != "DSH_HOME"
        }
        env["NO_COLOR"] = "1"
        native_config = self.computer.project_root / self.settings.get(
            "native_config", "config/harness/executor.local.json"
        )
        args = [str(native_config)] if native_config.is_file() else []
        self.process = await asyncio.create_subprocess_exec(
            str(self.settings["node_path"]),
            str(Path(__file__).with_name("harness") / "task-agent.mjs"),
            str(self.settings["runtime_root"]),
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.settings["runtime_root"],
            env=env,
            creationflags=CREATION_FLAGS,
            limit=4_000_000,
        )
        ready = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 20))
        if ready != {"kind": "ready", "version": "0.2.1-rc.2"}:
            raise RuntimeError("harness_unavailable")

    async def _stop_process(self):
        if self.process and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        self.process = None

    async def _model(self, task, options, *, completion_only=False):
        row = self.store.get(task)
        limits = json.loads(row["contract"])["limits"]
        if row["state"] != "running" or row["calls"] >= limits["calls"]:
            return {"error": "task_stopped_or_call_limit"}
        messages = model_messages(options["messages"])
        for message in messages:
            if message['role'] == 'tool':
                # Put the canonical ID next to the result, so the model need not
                # infer a label such as pwsh-1 from its own prose.
                message['content'] = json.dumps({'host_receipt_id': message['tool_call_id'],
                    'result': message['content']}, ensure_ascii=False)
        request = ModelRequest(
            messages=messages,
            tools=[
                {"type": "function", "function": t} for t in options.get("tools", [])
            ],
            role="computer",
            purpose="computer_execution",
            prompt_version=row["prompt_hash"],
            max_tokens=4096,
            budget_tokens=256000
            if json.loads(row["contract"]).get("profile") == "full"
            else 64000,
            timeout=45,
            thinking=False,
        )
        estimate = (
            len(
                json.dumps(request.model_dump(mode="json"), ensure_ascii=False).encode()
            )
            + request.max_tokens
        )
        if row["tokens"] + estimate > limits["tokens"]:
            return {"error": "task_token_limit"}
        # Reserve locally before IO. Unknown calls retain this conservative charge.
        self.store.charge(task, 1, estimate)
        gateway = copy(self.gateway_for_scope(row["scope"]))
        gateway.semaphores = {**gateway.semaphores, "background": asyncio.Semaphore(1)}
        gateway.retries = 0
        gateway.request_observer = None
        try:
            async with asyncio.timeout(50):
                response = await gateway.generate(request)
        except Exception:
            return {"error": "model_unavailable_or_budget_exhausted"}
        charged = gateway._charged_tokens(response, estimate)
        self.store.charge(task, 0, charged - estimate)
        if completion_only and any(c['function']['name'] != 'finish_task' for c in response.tool_calls):
            return {'error': 'finish_task_required_no_new_actions'}
        if json.loads(row['contract']).get('profile') == 'full' and not response.tool_calls:
            if response.content:
                self.store.save_draft(task, response.content)
            if not completion_only:
                # One bounded, budgeted closure request; expose only finish_task
                # so a missing completion call cannot rerun successful actions.
                closure = {'role': 'user', 'content': [{'type': 'text', 'text':
                    '宿主收尾提醒：报告已经保留，但任务尚未完成校验。现在只调用finish_task，'
                    '用真实回执核验并总结；不要重新执行命令。不能验证则说明具体原因并用blocked。'
                    '真实回执ID：' + json.dumps(self.store.receipt_ids(task))}]}
                return await self._model(task, {'tools': [FULL_FINISH], 'messages': [
                    *options['messages'],
                    {'role': 'assistant', 'content': [{'type': 'text', 'text': response.content or ''}]},
                    closure]}, completion_only=True)
        blocks = []
        if response.content:
            blocks.append({"type": "text", "text": response.content})
        for call in response.tool_calls:
            blocks.append(
                {
                    "type": "tool-call",
                    "id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                }
            )
        return {"blocks": blocks}

    def _tool(self, task, packet, tools, count):
        row = self.store.get(task)
        if (
            row["state"] != "running"
            or count > json.loads(row["contract"])["limits"]["tools"]
        ):
            return {"error": "task_stopped_or_step_limit", "end_turn": True}
        name, args = packet["name"], packet["args"]
        available = (
            [FULL_FINISH]
            if json.loads(row["contract"]).get("profile") == "full"
            else TOOLS
        )
        definition = next((t for t in available if t["name"] == name), None)
        if definition is None:
            return {"error": "tool_not_allowed", "end_turn": True}
        try:
            jsonschema.validate(args, definition["parameters"])
        except (ValueError, jsonschema.ValidationError):
            return {"error": "invalid_tool_arguments"}
        execute, cached = self.store.claim_call(task, packet["call_id"], name, args)
        if not execute:
            return cached
        unknown = False
        try:
            tools.deadline = time.monotonic() + 20
            if name == "finish_task":
                if isinstance(tools, FullComputerTools) and args['status'] == 'completed':
                    checks = []
                    for check in args['checks']:
                        try:
                            checks.append(tools.verify(check))
                        except (ValueError, KeyError, OSError):
                            checks.append({'kind': check['kind'], 'passed': False,
                                           'error': 'check_or_receipt_not_found'})
                    if not checks or not all(c['passed'] for c in checks):
                        failures = self.store.completion_errors(task) + 1
                        result = {'error': 'completion_verification_failed', 'checks': checks,
                                  'available_receipt_ids': self.store.receipt_ids(task),
                                  'suggestion': '修正完成检查后再次调用finish_task；从host_receipt_id或available_receipt_ids逐字复制真实call_id，不能编造pwsh-1；只需修正引用，不要重做已成功操作。空checks不能确认完成。',
                                  'end_turn': failures >= 3}
                        if failures >= 3:
                            result['state'] = 'blocked'
                            result['reason'] = 'completion_verification_failed'
                            self.store.finish(task, packet['call_id'], result,
                                args['summary'] + '\n宿主说明：完成校验连续失败，需修正回执引用或检查项，不代表用户缺少授权。')
                        else:
                            self.store.receipt(task, packet['call_id'], result)
                        return result
                else:
                    checks = [tools.verify(check) for check in args["checks"]]
                complete = (
                    args["status"] == "completed"
                    and bool(checks)
                    and all(c["passed"] for c in checks)
                )
                result = {
                    "state": "completed" if complete else "blocked",
                    "checks": checks,
                    "end_turn": True,
                }
                self.store.finish(task, packet["call_id"], result, args["summary"])
                return result
            else:
                result = getattr(tools, name)(**args)
        except (
            ValueError,
            KeyError,
            UnicodeError,
            FileExistsError,
            FileNotFoundError,
            IsADirectoryError,
            NotADirectoryError,
        ):
            result = {"error": "action_rejected_or_check_unavailable"}
        except Exception:
            unknown = True
            result = {"error": "outcome_unknown", "end_turn": True}
            self.store.state(task, "unknown", "工具结果不明；已停止，禁止自动重放。")
        self.store.receipt(task, packet["call_id"], result, unknown=unknown)
        return result

    def _native_claim(self, task, packet):
        row = self.store.get(task)
        contract = json.loads(row["contract"])
        catalog = self.store.catalog(task)
        if (
            contract.get("profile") != "full"
            or row["state"] != "running"
            or packet["name"] not in catalog
        ):
            return {"allow": False}
        jsonschema.validate(packet["args"], catalog[packet["name"]]["parameters"])
        if self.store.tool_count(task) >= contract["limits"]["tools"]:
            return {"allow": False}
        execute, cached = self.store.claim_call(
            task, packet["call_id"], packet["name"], packet["args"]
        )
        if not execute:
            if cached.get("error") == "outcome_unknown":
                self.store.state(
                    task, "unknown", "存在结果不明的工具调用，不自动重放。"
                )
                return {"allow": False}
            return {"cached": cached}
        return {"allow": True}

    async def run(self, task):
        row = self.store.get(task)
        contract = json.loads(row["contract"])
        if (
            row["owner"] != self.computer.owner_id
            or not self.computer.enabled
            or not self.computer.live
        ):
            self.store.state(task, "blocked", "电脑执行当前未授权。")
            return
        full = contract.get("profile") == "full"
        tools = (
            FullComputerTools(contract["roots"], self.store, task)
            if full
            else ComputerTools(contract["roots"])
        )
        await self._start_process()
        await self._send(
            {
                "kind": "run",
                "profile": contract.get("profile", "bounded"),
                "cwd": contract["roots"]["workspace"] if full else None,
                "task": task,
                "events": self.store.events(task),
                "prompt": contract["prompt"],
                "message_id": f"{task}-{row['turn']}",
                "goal": json.dumps(
                    {
                        "contract": {
                            k: v for k, v in contract.items() if k != "prompt"
                        },
                        "current_input": self.store.input(task),
                    },
                    ensure_ascii=False,
                ),
                "tools": [FULL_FINISH] if full else TOOLS,
            }
        )
        count = self.store.tool_count(task)
        started = time.monotonic()
        read_task = None
        cancel_sent = False
        try:
            while True:
                row = self.store.get(task)
                if (
                    row["state"] == "running"
                    and time.monotonic() - started
                    > contract["limits"]["seconds_per_turn"]
                ):
                    self.store.state(task, "pausing", "本轮时限到达，等待人工继续。")
                    row = self.store.get(task)
                if (
                    row["state"] in {"pausing", "cancelling", "unknown"}
                    and not cancel_sent
                ):
                    await self._send({"kind": "cancel", "task": task})
                    cancel_sent = True
                if read_task is None:
                    read_task = asyncio.create_task(self.process.stdout.readline())
                try:
                    line = await asyncio.wait_for(asyncio.shield(read_task), 1)
                except TimeoutError:
                    continue
                read_task = None
                packet = json.loads(line)
                if packet.get("task") != task:
                    raise RuntimeError("task_ipc_mismatch")
                kind = packet["kind"]
                if kind == "event":
                    self.store.event(task, packet["event"])
                elif kind == "catalog":
                    if not full:
                        raise ValueError("unexpected_native_catalog")
                    self.store.catalog(task, packet["tools"])
                    await self._send(
                        {"kind": "reply", "id": packet["id"], "result": {"ok": True}}
                    )
                elif kind == "native_claim":
                    result = self._native_claim(task, packet)
                    if not result.get("allow") and not result.get("cached"):
                        await self._send({"kind": "cancel", "task": task})
                    await self._send(
                        {"kind": "reply", "id": packet["id"], "result": result}
                    )
                elif kind == "native_result":
                    self.store.receipt(task, packet["call_id"], packet["result"])
                    await self._send(
                        {"kind": "reply", "id": packet["id"], "result": {"ok": True}}
                    )
                elif kind in {"model", "tool"}:
                    row = self.store.get(task)
                    if (
                        time.monotonic() - started
                        > contract["limits"]["seconds_per_turn"]
                    ):
                        self.store.state(
                            task, "pausing", "本轮时限到达，等待人工继续。"
                        )
                        row = self.store.get(task)
                    if row["state"] in {"pausing", "cancelling", "unknown"}:
                        await self._send({"kind": "cancel", "task": task})
                        result = {"error": "host_stopped", "end_turn": True}
                    elif kind == "model":
                        result = await self._model(task, packet["options"])
                    else:
                        count += 1
                        # Await completion before acknowledging pause/cancel or releasing desktop ownership.
                        result = await asyncio.to_thread(
                            self._tool,
                            task,
                            packet,
                            tools,
                            self.store.tool_count(task) + 1,
                        )
                    await self._send(
                        {"kind": "reply", "id": packet["id"], "result": result}
                    )
                elif kind in {"done", "error"}:
                    break
                else:
                    raise RuntimeError("unsupported_harness_packet")
        finally:
            if read_task:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
        state = self.store.get(task)["state"]
        if state in {"running", "pausing", "cancelling"}:
            draft = self.store.get(task)['summary']
            self.store.state(
                task,
                {"running": "blocked", "pausing": "paused", "cancelling": "cancelled"}[
                    state
                ],
                (draft + '\n宿主说明：执行回合已停止，尚未提交有效完成校验；以上为未验证报告。')
                if state == 'running' and draft else "执行回合已停止；以工具回执及独立检查为准。",
            )

    async def loop(self):
        while not self.stopping:
            if not self.desktop_lock.acquire(blocking=False):
                await asyncio.sleep(0.3)
                continue
            task = None
            try:
                task = self.store.next()
                if task:
                    await self.run(task)
            except asyncio.CancelledError:
                if task:
                    self.store.state(
                        task, "unknown", "服务中断，未确认结果不得自动重放。"
                    )
                raise
            except Exception:
                if task:
                    self.store.state(
                        task, "unknown", "执行通道中断，未确认结果不得自动重放。"
                    )
                await self._stop_process()
            finally:
                self.desktop_lock.release()
            await asyncio.sleep(0.3)

    async def close(self):
        self.stopping = True
        # Finish bounded in-flight model/tool IO before killing the IPC process.
        if self.store:
            for row in self.store.list():
                if row["state"] in {"running", "pausing"}:
                    self.store.state(row["id"], "cancelling")
        if self.worker:
            try:
                await asyncio.wait_for(asyncio.shield(self.worker), 60)
            except TimeoutError:
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        await self._stop_process()
