"""Emotion is a sourced interpretation, never an independent experience root."""
from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from . import Contract, SourceSpan, digest, parse_ref, parse_timestamp


class PAD(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    valence: float = Field(default=0.0, ge=-1, le=1)
    arousal: float = Field(default=0.3, ge=0, le=1)
    dominance: float = Field(default=0.5, ge=0, le=1)

    def vector(self):
        return self.valence, self.arousal, self.dominance

    @classmethod
    def from_vector(cls, values):
        return cls(**dict(zip(("valence", "arousal", "dominance"), values, strict=True)))


class EmotionConfig(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    enabled: bool = True
    llm_appraisal: bool = True
    baseline: PAD = Field(default_factory=PAD)
    emotion_half_life_seconds: float = Field(default=1800, gt=0, le=31_536_000)
    mood_half_life_seconds: float = Field(default=21600, gt=0, le=31_536_000)
    emotion_alpha: float = Field(default=0.65, ge=0, le=1)
    mood_alpha: float = Field(default=0.2, ge=0, le=1)
    inertia: float = Field(default=0.5, ge=0, le=1)
    mood_recall: bool = False
    recall_weight: float = Field(default=0.05, ge=0, le=0.1)


class AppraisalMeaning(Contract):
    target: PAD
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    label: str = Field(min_length=1, max_length=60)
    meaning: str = Field(min_length=1, max_length=500)
    concern: str = Field(default="", max_length=240)


class EmotionAppraisal(AppraisalMeaning):
    algorithm: Literal["emotion-v1"] = "emotion-v1"
    event_ref: str
    persona_ref: str
    observed_at: str
    source_spans: list[SourceSpan] = Field(min_length=1, max_length=4)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value):
        return parse_timestamp(value).isoformat()

    @field_validator("event_ref", "persona_ref")
    @classmethod
    def exact_ref(cls, value):
        parse_ref(value)
        return value


def appraisal_id(event_ref):
    return "emotion_" + digest(["emotion-v1", event_ref])[:24]


class EmotionRetraction(Contract):
    appraisal_ref: str
    reason: str = Field(min_length=1, max_length=500)


def validate_retraction_sources(record, sources):
    value = EmotionRetraction.model_validate(record.payload)
    if (record.author_kind not in {"human", "human_import"} or record.revision != 1
            or record.record_id != "emotion_retract_" + digest(value.appraisal_ref)[:24]
            or value.appraisal_ref not in record.source_refs
            or sources[value.appraisal_ref].record_kind != "emotion_appraisal"):
        raise ValueError("emotion retraction requires a human decision and exact appraisal source")


def validate_appraisal_sources(record, sources):
    """Called at the archive boundary, including imports and non-agent writers."""
    appraisal = EmotionAppraisal.model_validate(record.payload)
    if record.record_id != appraisal_id(appraisal.event_ref) or record.revision != 1:
        raise ValueError("one immutable v1 emotion appraisal per event")
    required = {appraisal.event_ref, appraisal.persona_ref, *appraisal.evidence_refs,
                *(span.message_ref for span in appraisal.source_spans)}
    if not required.issubset(record.source_refs):
        raise ValueError("emotion sources must be pinned in record.source_refs")
    event = sources[appraisal.event_ref]
    if event.record_kind != "event" or event.payload.get("reality_scope") not in {"reported", "observed"}:
        raise ValueError("emotion appraisal requires a reported/observed event")
    if sources[appraisal.persona_ref].record_kind != "persona_version":
        raise ValueError("emotion persona source must be persona_version")
    event_spans = event.payload.get("source_spans", [])
    times = []
    for span in appraisal.source_spans:
        raw = sources[span.message_ref]
        if (raw.record_kind != "raw_message" or raw.payload.get("role") != "user"
                or span.start >= span.end or raw.body_markdown[span.start:span.end] != span.quote
                or not any(e["message_ref"] == span.message_ref and e["start"] <= span.start
                           and span.end <= e["end"] for e in event_spans)):
            raise ValueError("emotion quote must be within an exact original event span")
    for span in event_spans:
        times.append(parse_timestamp(sources[span["message_ref"]].created_at))
    if not times or parse_timestamp(appraisal.observed_at) != max(times):
        raise ValueError("emotion observed_at must be anchored to original message receipt")
