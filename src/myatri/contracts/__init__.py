"""Versioned public contracts. References always pin an immutable revision."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value: str, *, default_zone: str | None = None) -> datetime:
    """Normalize an instant; only legacy callers may supply a local timezone."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        if default_zone is None:
            raise ValueError("timestamp must include a timezone")
        from zoneinfo import ZoneInfo
        moment = moment.replace(tzinfo=ZoneInfo(default_zone))
    return moment.astimezone(timezone.utc)


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def parse_ref(ref: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Za-z0-9_-]{1,160})@([1-9][0-9]*)", ref)
    if not match:
        raise ValueError(f"invalid versioned reference: {ref!r}")
    return match[1], int(match[2])


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


KINDS = {"raw_message", "event", "interpretation", "reflection", "theme",
         "temporary_state", "activation", "behavior_attempt", "persona_version",
         "persona_change", "attention_item", "summary", "cognitive_run", "edge",
         "cluster_snapshot", "index_generation", "usage", "emotion_appraisal", "emotion_retraction"}


class Record(Contract):
    record_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    revision: int = Field(default=1, ge=1)
    schema_version: Literal["1.0"] = "1.0"
    record_kind: str
    agent_id: str = Field(default="atri", min_length=1)
    user_id: str = Field(default="local", min_length=1)
    created_at: str = Field(default_factory=utcnow)
    author_kind: Literal["human", "system", "model", "human_import"] = "system"
    source_refs: list[str] = Field(default_factory=list)
    supersedes_revision: int | None = None
    generator: dict[str, Any] | None = None
    body_markdown: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_record(self):
        if self.record_kind not in KINDS:
            raise ValueError("unknown record kind")
        moment = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        if moment.tzinfo is None or moment.utcoffset().total_seconds() != 0:
            raise ValueError("created_at must be UTC with timezone")
        for ref in self.source_refs:
            parse_ref(ref)
        if self.revision > 1 and self.supersedes_revision != self.revision - 1:
            raise ValueError("new revision requires immediately preceding supersedes_revision")
        expected = digest({"body_markdown": self.body_markdown, "payload": self.payload})
        if self.content_hash and self.content_hash != expected:
            raise ValueError("content hash mismatch")
        object.__setattr__(self, "content_hash", expected)
        if self.author_kind == "model" and not all(
                self.generator and self.generator.get(k) for k in ("provider", "model", "prompt_version", "run_id")):
            raise ValueError("model records require complete generator metadata")
        if self.record_kind not in {"raw_message", "persona_version", "cognitive_run", "usage", "index_generation"} and not self.source_refs:
            raise ValueError("derived record requires versioned sources")
        return self

    @property
    def ref(self) -> str:
        return f"{self.record_id}@{self.revision}"


class SourceSpan(Contract):
    message_ref: str
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    quote: str


class Event(Contract):
    occurred_at: str | None = None
    time_precision: str = "unknown"
    participants: list[str] = Field(default_factory=list)
    fact_summary: str
    source_spans: list[SourceSpan] = Field(min_length=1)
    event_fingerprint: str
    reality_scope: Literal["reported", "observed", "fictional", "quoted", "role_output"] = "reported"


class TemporaryEntry(Contract):
    text: str
    kind_hint: str = "thought"
    scope: str = "current conversation"
    origin_refs: list[str] = Field(default_factory=list)
    origin_kind: Literal["experience", "generated_idea"] = "experience"
    tentative: bool = True
    status: Literal["open", "paused", "settled", "discarded", "consolidated"] = "open"
    review_at: str | None = None
    last_relevant_at: str | None = None
    reason: str = ""
    persona_change_ref: str | None = None

    @field_validator("review_at", "last_relevant_at")
    @classmethod
    def aware_time(cls, value):
        return parse_timestamp(value).isoformat() if value is not None else None

    @model_validator(mode="after")
    def origin_and_consolidation(self):
        if not self.origin_refs:
            raise ValueError("state must reference experience or a generation run")
        if self.status == "consolidated" and not self.persona_change_ref:
            raise ValueError("consolidation requires committed PersonaChange")
        return self


class ActivationSnapshot(Contract):
    context_snapshot: str | None
    entry_refs: list[str]
    selected_refs: list[str]
    reasons: dict[str, str]
    saliency_components: dict[str, dict[str, float]] = Field(default_factory=dict)
    selection_method: str = "heuristic"
    limitations: list[str] = Field(default_factory=list)
    generator: dict[str, Any] | None = None


class BehaviorAttempt(Contract):
    intent_ref: str
    decision_status: Literal["planned", "draft", "expressed", "attempted", "blocked", "unknown"]
    action_refs: list[str] = Field(default_factory=list)
    occurred_at: str | None = None
    observed_feedback_refs: list[str] = Field(default_factory=list)
    outcome_unknown: bool = True

    @field_validator("occurred_at")
    @classmethod
    def aware_action_time(cls, value):
        return parse_timestamp(value).isoformat() if value is not None else None

    @model_validator(mode="after")
    def actual_action(self):
        if self.decision_status in {"expressed", "attempted"} and not self.action_refs:
            raise ValueError("action needs persisted evidence")
        if not self.observed_feedback_refs and not self.outcome_unknown:
            raise ValueError("no feedback means unknown outcome")
        return self


class PersonaPatch(Contract):
    base_revision: int = Field(ge=1)
    entry_id: str
    old_hash: str
    replacement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    counter_refs: list[str] = Field(default_factory=list)
    temporary_refs: list[str] = Field(default_factory=list)
    attempt_refs: list[str] = Field(default_factory=list)
    counter_search_refs: list[str] = Field(default_factory=list)
    scope: str = "共同安排"


class Edge(Contract):
    edge_id: str
    from_ref: str
    to_ref: str
    type: Literal["similar", "supports", "contradicts", "follows_up", "reinterprets", "causal_hypothesis", "member_of"]
    direction: str = "directed"
    weight: float = Field(default=1.0, ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    status: str = "hypothesis"
    valid_from: str = Field(default_factory=utcnow)
    valid_to: str | None = None
    generator: dict[str, Any] | None = None


class RecallBundle(Contract):
    snapshot: str | None = None
    records: list[Record] = Field(default_factory=list)
    reasons: dict[str, list[str]] = Field(default_factory=dict)
    paths: list[list[str]] = Field(default_factory=list)
    counter_refs: list[str] = Field(default_factory=list)
    index_watermark: str | None = None
    limitations: list[str] = Field(default_factory=list)


class ModelRequest(Contract):
    messages: list[dict[str, Any]]
    role: str = "foreground"
    prompt_version: str = "v1"
    purpose: str = "conversation"
    trace_id: str = Field(default_factory=lambda: uid("trace"))
    tools: list[dict[str, Any]] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    timeout: float = Field(default=45, gt=0)
    max_tokens: int = Field(default=1024, ge=1)
    budget_tokens: int = Field(default=16000, ge=1)
    estimated_input_tokens: int | None = Field(default=None, ge=1)
    thinking: bool = False
    reasoning_effort: Literal["low", "high", "max"] = "low"


class ModelResponse(Contract):
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str = "stop"
    provider: str
    model: str
    provider_fields: dict[str, Any] = Field(default_factory=dict)


class EmbeddingBatch(Contract):
    vectors: list[list[float]]
    space_id: str
    model: str
    model_revision: str
    dimension: int = Field(ge=1)
    normalization: str = "l2"

    @model_validator(mode="after")
    def valid_dimensions(self):
        import math
        if any(len(v) != self.dimension or not all(math.isfinite(x) for x in v) for v in self.vectors):
            raise ValueError("embedding dimensions/non-finite values")
        return self


class ChatProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str], space_id: str) -> EmbeddingBatch: ...


class Proposal(Contract):
    proposal_id: str = Field(default_factory=lambda: uid("proposal"))
    base_snapshot: str | None
    read_versions: dict[str, int]
    records: list[Record]
    role: str
    run_id: str


class ClusterSnapshot(Contract):
    cluster_run_id: str
    graph_generation: str
    algorithm: str = "leiden"
    algorithm_version: str
    params: dict[str, Any]
    seed: int
    members: dict[str, list[str]]
    parent: dict[str, str] = Field(default_factory=dict)
    lineage: list[dict[str, Any]] = Field(default_factory=list)
    quality: dict[str, Any] = Field(default_factory=dict)


class JobSpec(Contract):
    role: str
    input_refs: list[str] = Field(default_factory=list)
    prompt_version: str = "v1"
    target_versions: dict[str, int] = Field(default_factory=dict)
    priority: int = 10
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def idempotency_key(self):
        return digest({"role": self.role, "refs": sorted(self.input_refs),
                       "prompt": self.prompt_version, "targets": self.target_versions, "payload": self.payload})
