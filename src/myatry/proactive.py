"""主动发言调度器（方向 3 / 3B：自发起话题）。

本模块是**独立可测**的调度器：不 import bridge 私有实现，所有外部能力都由构造参数
注入。桥接侧只负责把数据库、引擎与投递状态机接到这些回调上。

核心不变量（对应设计文档 3.2）：

- 主动轮生成的记录**不是人类证据**。它的 metadata 必须带
  ``self_initiated=True`` / ``author_kind="self_initiated"`` /
  ``reality_scope="internal"``；写入侧仍用 ``role="user"`` 只是为了复用现有
  ``respond()`` 流程。任何缺少这些标记的草稿都会被调度器拒绝投递。
- 主动发言自己的记录**不能回流成候选**。候选来源若返回 self_initiated 记录，
  视为来源污染并跳过。
- 沉默优先：任一闸门不过就静默跳过，不重试、不补偿。跳过的原因写进诊断。

父代理接线需要提供的回调（详见各构造参数）：

- ``state(scope)`` 返回 :class:`ProactiveState`；
- ``candidates(scope)`` 返回 :class:`Candidate` 列表；
- ``generate(*, scope, candidate, now, instruction)`` 返回草稿 dict；
- ``deliver(*, scope, candidate, draft, now)`` 创建 ``trigger_kind='proactive'``
  的投递并返回 ``{"id": ..., "status": ...}``；
- ``clock()`` 返回带时区的时间；
- ``diagnostic(record)`` 落诊断（可用 :class:`JsonlDiagnostics`）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
import inspect
import json
from pathlib import Path
import random
from typing import Any, Callable, Mapping

from .proactive_prompt import render_proactive_instructions

#: 主动轮在存档里的 turn_kind 取值（与设计文档 3.2 一致）。
TURN_KIND_INITIATE = "initiate"

#: 生成记录必须带的显式标记；父代理把它作为 ``metadata`` 传给 ``respond()``。
#: ``role="user"`` 由父代理单独传给 ``append_message``，不放进这里，避免重复键。
SELF_INITIATED_METADATA: dict[str, Any] = {
    "author_kind": "self_initiated",
    "reality_scope": "internal",
    "self_initiated": True,
    "turn_kind": TURN_KIND_INITIATE,
}

#: 候选优先级（设计文档 3.7 的四级，数值越小越优先）。
#: ``group_revive``（群沉寂后的主动暖场）与 ``interesting_exchange``
#: （正在热议、可选择性插话）是后续加入的"更像真人"方向。
CANDIDATE_PRIORITY = {
    "group_revive": 1,
    "failed_explicit": 1,
    "interesting_exchange": 2,
    "deferred_topic": 2,
    "temporary_entry": 3,
    "unanswered_question": 4,
}

#: 配置默认值；``[proactive]`` 段只需覆盖想改的键。
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "shadow_mode": True,
    "kill_switch_path": "var/KILL_PROACTIVE",
    "active_start": "09:00",
    "active_end": "23:30",
    "min_interval_minutes": 20.0,
    "max_interval_minutes": 90.0,
    # >0 时判定循环按固定节奏跑（例如 10 分钟一次"有没有感兴趣的对话"）；
    # 0 保持旧的 20–90 分钟随机采样。
    "tick_interval_minutes": 0.0,
    "speech_cooldown_minutes": 15.0,
    "daily_limit": 10,
    "group_silence_hours": 2.0,
    # >0 时群沉寂达到该小时数后允许一次"主动暖场"候选（每天至多一次）。
    "revive_silence_hours": 0.0,
    # 感兴趣对话的判定：窗口内消息数达到阈值，且命中关键词或问句。
    "interest_burst_messages": 4,
    "interest_window_minutes": 10.0,
    "interest_keywords": [],
    "budget_limit_ratio": 0.8,
    "candidate_ttl_seconds": 45.0,
}

#: 单次投递最多分段数，与现有投递状态机的 ``part_index`` 上界一致。
MAX_REPLY_PARTS = 3


def is_self_initiated(payload: Mapping[str, Any] | None) -> bool:
    """判断一条记录是否由主动轮发起。

    任一标记成立即视为主动轮记录，宁可多判也不漏判。

    Args:
        payload: 待判断的记录 / metadata / 候选 payload。

    Returns:
        是主动轮记录则返回 True。
    """
    if not isinstance(payload, Mapping):
        return False
    return (
        payload.get("self_initiated") is True
        or payload.get("author_kind") == "self_initiated"
        or payload.get("turn_kind") == TURN_KIND_INITIATE
    )


def is_listening_evidence(payload: Mapping[str, Any] | None) -> bool:
    """判断一条记录能否作为人类旁听证据（``listening_seen`` / A1 候选）。

    父代理必须在写短记忆与提炼候选之前调用本函数过滤；主动轮记录一律返回 False。

    Args:
        payload: 待判断的写入记录。

    Returns:
        可以作为人类证据则返回 True。
    """
    if not isinstance(payload, Mapping):
        return False
    if is_self_initiated(payload):
        return False
    if payload.get("role") == "assistant":
        return False
    bot_id = payload.get("bot_id")
    if bot_id and payload.get("sender_id") == bot_id:
        return False
    return True


# 显式断言：主动轮的 metadata 永远不是人类证据。
assert not is_listening_evidence(SELF_INITIATED_METADATA), (
    "self_initiated records must never count as human evidence"
)


class DraftRejected(ValueError):
    """草稿不符合"不伪造人类证据"的契约；``reason`` 是稳定的诊断码。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CandidateRejected(ValueError):
    """候选本身不可用（类型未知、时间缺失、自我污染等）。"""


@dataclass(frozen=True)
class Candidate:
    """一个"现在可以开口"的候选。

    Attributes:
        candidate_id: 候选标识（消息 ID 或临时条目 ID），必须唯一。
        kind: 候选类型，取值见 :data:`CANDIDATE_PRIORITY`。
        created_at: **候选就绪时间**，TTL 从这个时刻开始算，不是原消息时间。
        reason: "为什么是这句"的人类可读说明，会写进生成指令与诊断。
        context_hash: 使用的上下文摘要哈希，便于事后回查。
        excerpt: 相关上下文摘录，仅作语气参考。
        source_at: 原始消息时间，可空，仅供追溯。
        payload: 原始记录，用于检查是否被自我污染。
    """

    candidate_id: str
    kind: str
    created_at: datetime
    reason: str = ""
    context_hash: str = ""
    excerpt: str = ""
    source_at: datetime | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProactiveState:
    """调度器做判定所需的外部状态快照。

    Attributes:
        scope: 群作用域（32 位十六进制），仅用于诊断。
        last_speech_at: 本群机器人上一次任意发言（含被动回复）的时间。
        proactive_today: 当日已投递的主动发言条数。
        group_last_message_at: 本群最后一条人类消息时间。
        budget_used_ratio: 当日预算已用比例（0~1），未知用 None。
        explicit_reply_pending: 是否存在尚未完成的明确呼叫（用于 superseded 判定）。
    """

    scope: str = ""
    last_speech_at: datetime | None = None
    proactive_today: int = 0
    group_last_message_at: datetime | None = None
    budget_used_ratio: float | None = None
    explicit_reply_pending: bool = False
    #: 群已沉寂超过 revive_silence_hours 且今天的暖场名额未用：本 tick 允许
    #: 越过"群静默"闸门发起一次 group_revive。
    revive_available: bool = False


@dataclass(frozen=True)
class ProactiveDraft:
    """生成回调返回的草稿（已校验）。

    Attributes:
        parts: 要发送的文本分段（1~3 段，每段非空）。
        metadata: 生成记录的 metadata，必须含 self_initiated 标记。
        record_id: 落库后的记录 ID，可空。
        context_hash: 实际使用的上下文摘要哈希，可空。
    """

    parts: tuple[str, ...]
    metadata: Mapping[str, Any]
    record_id: str = ""
    context_hash: str = ""


def _as_flag(values: Mapping[str, Any], key: str) -> bool:
    value = values.get(key, DEFAULTS[key])
    if not isinstance(value, bool):
        raise ValueError(f"主动发言配置 {key} 必须是布尔值")
    return value


def _as_number(
    values: Mapping[str, Any],
    key: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = values.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"主动发言配置 {key} 必须是数字")
    if minimum is not None and value < minimum:
        raise ValueError(f"主动发言配置 {key} 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"主动发言配置 {key} 不能大于 {maximum}")
    return float(value)


def _as_clock_time(values: Mapping[str, Any], key: str) -> time:
    value = values.get(key, DEFAULTS[key])
    if not isinstance(value, str):
        raise ValueError(f"主动发言配置 {key} 必须是 HH:MM 字符串")
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        raise ValueError(f"主动发言配置 {key} 必须是 HH:MM，收到 {value!r}") from None
    if parsed.tzinfo is not None:
        raise ValueError(f"主动发言配置 {key} 不能带时区")
    return parsed


def _aware(value: datetime, reference: datetime) -> datetime:
    """把无时区时间按参照时间补上时区；带时区的原样返回。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


async def _resolve(value: Any) -> Any:
    """同步/异步回调都接受：可等待对象就 await，其余直接返回。"""
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True)
class ProactiveSettings:
    """``[proactive]`` 段的强类型视图。

    所有字段都可缺省；缺省值见 :data:`DEFAULTS`。未知键会被拒绝，避免配置写错却静默生效。
    """

    enabled: bool = False
    shadow_mode: bool = True
    kill_switch_path: Path = Path("var/KILL_PROACTIVE")
    active_start: time = time(9, 0)
    active_end: time = time(23, 30)
    min_interval_minutes: float = 20.0
    max_interval_minutes: float = 90.0
    tick_interval_minutes: float = 0.0
    speech_cooldown_minutes: float = 15.0
    daily_limit: int = 10
    group_silence_hours: float = 2.0
    revive_silence_hours: float = 0.0
    interest_burst_messages: int = 4
    interest_window_minutes: float = 10.0
    interest_keywords: tuple[str, ...] = ()
    budget_limit_ratio: float = 0.8
    candidate_ttl_seconds: float = 45.0

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any] | None = None,
        *,
        base_dir: Path | str | None = None,
    ) -> ProactiveSettings:
        """从配置字典构造，并做范围校验。

        Args:
            config: ``[proactive]`` 段内容，可空。
            base_dir: 相对路径（kill switch 默认值）的基准目录，默认当前工作目录。

        Returns:
            校验通过的设置对象。

        Raises:
            ValueError: 出现未知键、类型错误或取值范围错误。
        """
        values = dict(config or {})
        unknown = set(values) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"未知的主动发言配置项：{', '.join(sorted(unknown))}")
        base = Path(base_dir) if base_dir is not None else Path.cwd()
        raw_kill = values.get("kill_switch_path", DEFAULTS["kill_switch_path"])
        if not isinstance(raw_kill, str) or not raw_kill.strip():
            raise ValueError("主动发言配置 kill_switch_path 必须是非空字符串")
        kill_path = Path(raw_kill).expanduser()
        if not kill_path.is_absolute():
            kill_path = base / kill_path
        start = _as_clock_time(values, "active_start")
        end = _as_clock_time(values, "active_end")
        if start >= end:
            raise ValueError("主动发言配置要求 active_start 早于 active_end")
        low = _as_number(values, "min_interval_minutes", minimum=1.0)
        high = _as_number(values, "max_interval_minutes", minimum=1.0)
        if low > high:
            raise ValueError("主动发言配置要求 min_interval_minutes 不大于 max_interval_minutes")
        limit = values.get("daily_limit", DEFAULTS["daily_limit"])
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("主动发言配置 daily_limit 必须是非负整数")
        burst = values.get("interest_burst_messages", DEFAULTS["interest_burst_messages"])
        if isinstance(burst, bool) or not isinstance(burst, int) or not 2 <= burst <= 20:
            raise ValueError("主动发言配置 interest_burst_messages 必须是 2–20 的整数")
        keywords = values.get("interest_keywords", DEFAULTS["interest_keywords"])
        if (not isinstance(keywords, (list, tuple))
                or any(not isinstance(word, str) or not word.strip() for word in keywords)):
            raise ValueError("主动发言配置 interest_keywords 必须是非空字符串数组")
        return cls(
            enabled=_as_flag(values, "enabled"),
            shadow_mode=_as_flag(values, "shadow_mode"),
            kill_switch_path=kill_path,
            active_start=start,
            active_end=end,
            min_interval_minutes=low,
            max_interval_minutes=high,
            tick_interval_minutes=_as_number(values, "tick_interval_minutes",
                                             minimum=0.0, maximum=360.0),
            speech_cooldown_minutes=_as_number(values, "speech_cooldown_minutes", minimum=0.0),
            daily_limit=limit,
            group_silence_hours=_as_number(values, "group_silence_hours", minimum=0.0),
            revive_silence_hours=_as_number(values, "revive_silence_hours",
                                            minimum=0.0, maximum=24.0),
            interest_burst_messages=burst,
            interest_window_minutes=_as_number(values, "interest_window_minutes",
                                               minimum=1.0, maximum=60.0),
            interest_keywords=tuple(word.strip() for word in keywords),
            budget_limit_ratio=_as_number(values, "budget_limit_ratio", minimum=0.0, maximum=1.0),
            candidate_ttl_seconds=_as_number(values, "candidate_ttl_seconds", minimum=1.0),
        )


class JsonlDiagnostics:
    """把调度器诊断写成 JSONL；草稿单独落一份供人工审阅。

    只写调度器给出的结构化记录，不写群聊原文之外的额外数据。
    """

    def __init__(
        self,
        base_dir: Path | str,
        *,
        log_name: str = "var/diagnostics/proactive.jsonl",
        draft_name: str = "var/diagnostics/proactive-drafts.jsonl",
    ) -> None:
        base = Path(base_dir)
        self.log_path = base / log_name
        self.draft_path = base / draft_name

    def __call__(self, record: Mapping[str, Any]) -> None:
        """追加一条诊断记录；``proactive_draft`` 事件写进草稿文件。"""
        path = self.draft_path if record.get("event") == "proactive_draft" else self.log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


class ProactiveScheduler:
    """按闸门顺序判定一次"该不该主动开口"，通过则生成并投递。

    单个实例服务一个 scope；多群请各建一个实例（计数与冷却都按 scope 归属）。
    """

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        scope: str,
        state: Callable[[str], Any],
        candidates: Callable[[str], Any],
        generate: Callable[..., Any],
        deliver: Callable[..., Any],
        clock: Callable[[], datetime],
        diagnostic: Callable[[Mapping[str, Any]], Any],
        base_dir: Path | str | None = None,
        rng: random.Random | None = None,
    ) -> None:
        """构造调度器。

        Args:
            config: ``[proactive]`` 段配置，默认见 :data:`DEFAULTS`。
            scope: 群作用域（32 位十六进制），只会传给回调，不做业务解释。
            state: ``state(scope) -> ProactiveState | dict``，可同步可异步。
            candidates: ``candidates(scope) -> Sequence[Candidate | dict]``。
            generate: ``generate(*, scope, candidate, now, instruction) -> 草稿``；
                草稿是 dict（``text``/``parts``、``metadata``、``record_id``、
                ``context_hash``）或 :class:`ProactiveDraft`。
            deliver: ``deliver(*, scope, candidate, draft, now) -> dict``，
                必须返回 ``{"id": ..., "status": ...}``。
            clock: 返回带时区 ``datetime`` 的时钟，测试可冻结。
            diagnostic: ``diagnostic(record)``，落诊断（可同步可异步）。
            base_dir: 相对路径基准目录，默认当前工作目录。
            rng: 间隔采样用的随机源，测试可固定种子。

        Raises:
            ValueError: 配置非法或 scope 形状不对。
        """
        if not isinstance(scope, str) or len(scope) != 32 or any(
            char not in "0123456789abcdef" for char in scope
        ):
            raise ValueError("主动发言 scope 必须是 32 位小写十六进制")
        self.scope = scope
        self.settings = ProactiveSettings.from_config(config, base_dir=base_dir)
        self._state = state
        self._candidates = candidates
        self._generate = generate
        self._deliver = deliver
        self._clock = clock
        self._diagnostic = diagnostic
        self._rng = rng or random.Random()
        self._used: set[str] = set()
        self._stopped = False
        self._next_at: datetime | None = None

    # ------------------------------------------------------------------ 时间

    def now(self) -> datetime:
        """读取冻结/真实时钟，并强制要求带时区。"""
        value = self._clock()
        if not isinstance(value, datetime):
            raise ValueError("主动发言时钟必须返回 datetime")
        if value.tzinfo is None:
            raise ValueError("主动发言时钟必须返回带时区的时间")
        return value

    @property
    def next_at(self) -> datetime | None:
        """下一次计划判定时间（由 :meth:`run` 维护），未启动时为 None。"""
        return self._next_at

    def sample_interval_seconds(self) -> float:
        """采样两次判定之间的等待秒数。

        用指数分布（泊松到达的间隔）取 20–90 分钟，超过上限按上限截断，避免长尾。

        Returns:
            介于 ``min_interval_minutes`` 与 ``max_interval_minutes`` 之间的秒数。
        """
        low = self.settings.min_interval_minutes * 60
        high = self.settings.max_interval_minutes * 60
        span = high - low
        if span <= 0:
            return low
        return min(high, low + self._rng.expovariate(1.0 / span))

    # ------------------------------------------------------------- 生命周期

    async def run(self) -> None:
        """后台循环：等待采样间隔后执行一次 :meth:`tick`。

        ``enabled`` 为假时直接返回（且不执行任何判定），保证默认关闭时不启动。
        """
        if not self.settings.enabled:
            await self._emit({
                "event": "proactive_disabled",
                "scope": self.scope,
                "at": self.now().isoformat(),
            })
            return
        while not self._stopped:
            if self.settings.tick_interval_minutes > 0:
                # Fixed observation cadence (e.g. a 10-minute "anything
                # interesting going on?" pass); speech frequency stays bounded
                # by the cooldown, daily-limit and budget gates below.
                delay = self.settings.tick_interval_minutes * 60
            else:
                delay = self.sample_interval_seconds()
            self._next_at = self.now() + timedelta(seconds=delay)
            await asyncio.sleep(delay)
            if self._stopped:
                return
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # 判定出错只记录，不打死后台任务
                await self._emit({
                    "event": "proactive_error",
                    "scope": self.scope,
                    "at": self.now().isoformat(),
                    "error": type(error).__name__,
                })

    def stop(self) -> None:
        """请求循环退出；取消 ``asyncio`` 任务由调用方负责。"""
        self._stopped = True

    # ---------------------------------------------------------------- 判定

    async def tick(self) -> dict:
        """执行一次完整判定。

        闸门按设计文档 3.6 的顺序**短路**执行，任一不过即返回跳过原因，
        不重试、不补偿。通过全部闸门后才会选候选、生成、二次校验、投递。

        Returns:
            结构化结果：``spoke``（是否已进入投递；影子模式下恒为 False）、
            ``skipped``（跳过原因码，None 表示判定通过并已投递）、``detail``、
            ``candidate``、``delivery_id``、``delivery_status``、``shadow``、
            ``gates``（本轮实际通过的闸门名）。
        """
        now = self.now()
        gates: list[str] = []

        # 闸门 1：kill switch。存在即立即跳过，连状态都不读。
        if self.settings.kill_switch_path.is_file():
            return await self._finish(now, gates, skipped="kill_switch")

        # 闸门 2：全局开关，默认关闭。
        if not self.settings.enabled:
            return await self._finish(now, gates, skipped="disabled")
        gates.append("enabled")

        # 闸门 3：活跃时段。
        if not self._active(now):
            return await self._finish(now, gates, skipped="quiet_hours")
        gates.append("active_hours")

        state = await self._read_state()
        if state is None:
            # 状态读不到就当作"不确定"，沉默优先。
            return await self._finish(now, gates, skipped="state_unavailable")
        gates.append("state")

        # 闸门 4：距上次任何发言 ≥ 15 分钟。
        if state.last_speech_at is not None:
            last = _aware(state.last_speech_at, now)
            if now - last < timedelta(minutes=self.settings.speech_cooldown_minutes):
                return await self._finish(now, gates, skipped="speech_cooldown")
        gates.append("speech_cooldown")

        # 闸门 5：当日主动发言数上限。
        if state.proactive_today >= self.settings.daily_limit:
            return await self._finish(now, gates, skipped="daily_limit")
        gates.append("daily_limit")

        # 闸门 6：群内静默超过阈值则跳过——除非已具备"主动暖场"资格
        # （沉寂达到 revive_silence_hours 且今日名额未用），此时沉寂正是
        # 开口的理由。不知道最后消息时间仍视为静默跳过。
        if state.group_last_message_at is None:
            return await self._finish(now, gates, skipped="group_silent")
        silent = now - _aware(state.group_last_message_at, now)
        if silent > timedelta(hours=self.settings.group_silence_hours):
            if not state.revive_available:
                return await self._finish(now, gates, skipped="group_silent")
            gates.append("revive")
        gates.append("group_silent")

        # 闸门 7：预算余量必须 ≥ 20%（已用 < 80%）；未知同样视为不通过。
        if state.budget_used_ratio is None:
            return await self._finish(now, gates, skipped="budget_margin")
        if state.budget_used_ratio >= self.settings.budget_limit_ratio:
            return await self._finish(now, gates, skipped="budget_margin")
        gates.append("budget_margin")

        # 闸门 8：候选是否仍有效（TTL 45 秒）。
        candidate, reason = await self._select(now)
        if candidate is None:
            return await self._finish(now, gates, skipped=reason)
        gates.append("candidate")

        # ---- 生成 -----------------------------------------------------
        instruction = render_proactive_instructions(
            kind=candidate.kind,
            reason=candidate.reason,
            excerpt=candidate.excerpt,
            context_hash=candidate.context_hash,
        )
        try:
            raw = await _resolve(self._generate(
                scope=self.scope,
                candidate=candidate,
                now=now,
                instruction=instruction,
            ))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._used.add(candidate.candidate_id)
            return await self._finish(now, gates, skipped="generation_error", candidate=candidate)
        if raw is None:
            # 生成回调放弃（例如引擎判定不该开口），消费掉候选避免反复重试。
            self._used.add(candidate.candidate_id)
            return await self._finish(now, gates, skipped="generation_declined", candidate=candidate)
        try:
            draft = self._coerce_draft(raw)
        except DraftRejected as error:
            self._used.add(candidate.candidate_id)
            return await self._finish(
                now, gates, skipped="draft_rejected", candidate=candidate, detail=error.reason
            )
        self._used.add(candidate.candidate_id)

        # ---- 二次校验：TTL 与期间出现的明确呼叫 -------------------------
        later = self.now()
        if self.settings.kill_switch_path.is_file():
            return await self._finish(later, gates, skipped="kill_switch", candidate=candidate)
        if later - _aware(candidate.created_at, later) > timedelta(
            seconds=self.settings.candidate_ttl_seconds
        ):
            return await self._finish(later, gates, skipped="superseded", candidate=candidate)
        state_later = await self._read_state()
        if state_later is not None and state_later.explicit_reply_pending:
            return await self._finish(later, gates, skipped="superseded", candidate=candidate)

        # ---- 影子模式：只落草稿，不发送 --------------------------------
        if self.settings.shadow_mode:
            await self._emit({
                "event": "proactive_draft",
                "scope": self.scope,
                "at": later.isoformat(),
                "candidate": candidate.candidate_id,
                "candidate_kind": candidate.kind,
                "reason": candidate.reason,
                "parts": list(draft.parts),
                "metadata": dict(draft.metadata),
                "record_id": draft.record_id,
                "context_hash": draft.context_hash or candidate.context_hash,
            })
            return await self._finish(
                later, gates, skipped="shadow_mode", candidate=candidate, shadow=True
            )

        # ---- 投递 -----------------------------------------------------
        try:
            result = await _resolve(self._deliver(
                scope=self.scope,
                candidate=candidate,
                draft=draft,
                now=later,
            ))
        except asyncio.CancelledError:
            raise
        except Exception:
            # 发送前校验失败等异常：投递结果是 unknown，绝不自动重投。
            return await self._finish(
                later, gates, skipped="delivery_error", candidate=candidate,
                delivery_status="unknown",
            )
        if not isinstance(result, Mapping):
            return await self._finish(
                later, gates, skipped="delivery_invalid", candidate=candidate,
                delivery_status="unknown",
            )
        delivery_id = result.get("id")
        status = result.get("status")
        delivery_id = delivery_id if isinstance(delivery_id, str) and delivery_id else None
        status = status if isinstance(status, str) and status else "unknown"
        spoke = status in {"prepared", "sending", "sent"}
        return await self._finish(
            later,
            gates,
            skipped=None if spoke else "delivery_unknown",
            candidate=candidate,
            delivery_id=delivery_id,
            delivery_status=status,
        )

    # ------------------------------------------------------------- 内部实现

    def _active(self, now: datetime) -> bool:
        current = now.time()
        return self.settings.active_start <= current <= self.settings.active_end

    async def _read_state(self) -> ProactiveState | None:
        """读状态快照；任何异常或形状不对都返回 None（沉默优先）。"""
        try:
            raw = await _resolve(self._state(self.scope))
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if isinstance(raw, ProactiveState):
            return raw
        if not isinstance(raw, Mapping):
            return None

        def stamp(key: str) -> datetime | None:
            value = raw.get(key)
            if value is None:
                return None
            if not isinstance(value, datetime):
                raise ValueError("state timestamp must be datetime")
            return value

        try:
            today = raw.get("proactive_today", 0)
            if isinstance(today, bool) or not isinstance(today, int) or today < 0:
                return None
            ratio = raw.get("budget_used_ratio")
            if ratio is not None and (
                isinstance(ratio, bool) or not isinstance(ratio, (int, float))
            ):
                return None
            return ProactiveState(
                scope=str(raw.get("scope", self.scope)),
                last_speech_at=stamp("last_speech_at"),
                proactive_today=today,
                group_last_message_at=stamp("group_last_message_at"),
                budget_used_ratio=None if ratio is None else float(ratio),
                explicit_reply_pending=bool(raw.get("explicit_reply_pending", False)),
                revive_available=bool(raw.get("revive_available", False)),
            )
        except (TypeError, ValueError):
            return None

    async def _select(self, now: datetime) -> tuple[Candidate | None, str]:
        """按优先级选一个未过期、未被用过、未被自我污染的候选。"""
        try:
            raw = await _resolve(self._candidates(self.scope))
        except asyncio.CancelledError:
            raise
        except Exception:
            return None, "candidate_source_error"
        if raw is None:
            return None, "no_candidate"
        if not isinstance(raw, (list, tuple)):
            return None, "candidate_source_invalid"
        usable: list[Candidate] = []
        expired = 0
        invalid = 0
        polluted = 0
        for item in raw:
            try:
                candidate = self._coerce_candidate(item)
            except CandidateRejected as error:
                if str(error) == "candidate_self_initiated":
                    polluted += 1
                else:
                    invalid += 1
                continue
            if candidate.candidate_id in self._used:
                continue
            age = now - _aware(candidate.created_at, now)
            if age > timedelta(seconds=self.settings.candidate_ttl_seconds):
                expired += 1
                continue
            usable.append(candidate)
        if not usable:
            if polluted:
                return None, "candidate_source_polluted"
            if expired:
                return None, "candidate_expired"
            if invalid:
                return None, "candidate_invalid"
            return None, "no_candidate"
        usable.sort(
            key=lambda item: (
                CANDIDATE_PRIORITY[item.kind],
                -_aware(item.created_at, now).timestamp(),
            )
        )
        return usable[0], ""

    def _coerce_candidate(self, raw: Any) -> Candidate:
        if isinstance(raw, Candidate):
            candidate = raw
        elif isinstance(raw, Mapping):
            created = raw.get("created_at")
            if not isinstance(created, datetime):
                raise CandidateRejected("candidate_created_at")
            candidate = Candidate(
                candidate_id=raw.get("candidate_id", ""),
                kind=raw.get("kind", ""),
                created_at=created,
                reason=str(raw.get("reason", "")),
                context_hash=str(raw.get("context_hash", "")),
                excerpt=str(raw.get("excerpt", "")),
                source_at=raw.get("source_at"),
                payload=raw.get("payload") or {},
            )
        else:
            raise CandidateRejected("candidate_shape")
        if not isinstance(candidate.candidate_id, str) or not candidate.candidate_id.strip():
            raise CandidateRejected("candidate_id")
        if candidate.kind not in CANDIDATE_PRIORITY:
            raise CandidateRejected("candidate_kind")
        if not isinstance(candidate.created_at, datetime):
            raise CandidateRejected("candidate_created_at")
        if is_self_initiated(candidate.payload):
            # 主动发言自己的记录不得回流成候选：这是"不伪造人类证据"的硬边界。
            raise CandidateRejected("candidate_self_initiated")
        return candidate

    def _coerce_draft(self, raw: Any) -> ProactiveDraft:
        """校验草稿：纯文本 1~3 段，且必须带 self_initiated 标记。"""
        if isinstance(raw, ProactiveDraft):
            parts = list(raw.parts)
            metadata = dict(raw.metadata)
            record_id = raw.record_id
            context_hash = raw.context_hash
        elif isinstance(raw, Mapping):
            metadata = raw.get("metadata")
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, Mapping):
                raise DraftRejected("draft_metadata_invalid")
            text = raw.get("text")
            raw_parts = raw.get("parts", text)
            if isinstance(raw_parts, str):
                parts = [raw_parts]
            elif isinstance(raw_parts, (list, tuple)):
                parts = list(raw_parts)
            else:
                raise DraftRejected("draft_parts_invalid")
            record_id = raw.get("record_id", "")
            context_hash = raw.get("context_hash", "")
        else:
            raise DraftRejected("draft_shape")
        if not parts:
            raise DraftRejected("draft_empty")
        if any(not isinstance(part, str) or not part.strip() for part in parts):
            raise DraftRejected("draft_empty")
        if len(parts) > MAX_REPLY_PARTS:
            raise DraftRejected("draft_too_many_parts")
        metadata = dict(metadata)
        if metadata.get("role") not in {None, "user"}:
            raise DraftRejected("draft_role_invalid")
        if not is_self_initiated(metadata):
            raise DraftRejected("draft_missing_self_initiated")
        if metadata.get("author_kind") != "self_initiated":
            raise DraftRejected("draft_missing_author_kind")
        if metadata.get("reality_scope") != "internal":
            raise DraftRejected("draft_missing_reality_scope")
        if metadata.get("self_initiated") is not True:
            raise DraftRejected("draft_missing_self_initiated")
        return ProactiveDraft(
            parts=tuple(parts),
            metadata=metadata,
            record_id=str(record_id),
            context_hash=str(context_hash),
        )

    async def _finish(
        self,
        at: datetime,
        gates: list[str],
        *,
        skipped: str | None,
        candidate: Candidate | None = None,
        delivery_id: str | None = None,
        delivery_status: str | None = None,
        detail: str = "",
        shadow: bool | None = None,
    ) -> dict:
        """组装并落一条 tick 诊断，返回结构化结果。"""
        result = {
            "scope": self.scope,
            "at": at.isoformat(),
            "spoke": skipped is None,
            "skipped": skipped,
            "detail": detail,
            "candidate": candidate.candidate_id if candidate else None,
            "candidate_kind": candidate.kind if candidate else None,
            "priority": CANDIDATE_PRIORITY[candidate.kind] if candidate else None,
            "delivery_id": delivery_id,
            "delivery_status": delivery_status,
            "shadow": self.settings.shadow_mode if shadow is None else shadow,
            "gates": list(gates),
        }
        await self._emit({"event": "proactive_tick", **result})
        return result

    async def _emit(self, record: Mapping[str, Any]) -> None:
        try:
            await _resolve(self._diagnostic(record))
        except asyncio.CancelledError:
            raise
        except Exception:
            # 诊断写失败不能影响判定结论。
            pass
