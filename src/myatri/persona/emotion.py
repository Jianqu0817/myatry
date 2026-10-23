"""Read-only, snapshot-scoped affect projection over immutable appraisals.

No timer, LLM call, personality edit, evidence reinforcement or archive write
occurs while inspecting/decaying mood. Replay order follows source receipt.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from myatri._vendor.emotional_memory import lerp_pad, pad_distance, regress_pad, update_mood
from myatri.contracts import Proposal, Record, TemporaryEntry, digest, parse_timestamp
from myatri.contracts.emotion import EmotionAppraisal, EmotionConfig, PAD

#: 同一次心情变化的含义句只在变化后的这个窗口内随上下文提供（秒）。
#: 取情绪半衰期的一半：一段对话内足够说一次，之后不再反复提醒。
MEANING_FRESH_SECONDS = 2700


class EmotionState:
    def __init__(self, store, config=None):
        self.store = store
        self.config = config if isinstance(config, EmotionConfig) else EmotionConfig.model_validate(config or {})

    def read(self, *, snapshot=None, now=None):
        snapshot = snapshot or self.store.snapshot()
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("emotion clock must be timezone-aware")
        now = now.astimezone(timezone.utc)
        cfg = self.config
        records = self.store.list_records("emotion_appraisal", snapshot=snapshot) if cfg.enabled else []
        retracted = {r.payload["appraisal_ref"] for r in self.store.list_records("emotion_retraction", snapshot=snapshot)}
        records = [r for r in records if r.ref not in retracted]
        rows = [(EmotionAppraisal.model_validate(r.payload), r) for r in records]
        rows.sort(key=lambda pair: (parse_timestamp(pair[0].observed_at), pair[0].event_ref))
        affect = mood = cfg.baseline.vector()
        clock, seen, used, omitted_future = None, set(), [], 0
        latest = None
        for appraisal, record in rows:
            stamp = parse_timestamp(appraisal.observed_at)
            if stamp > now:
                omitted_future += 1
                continue
            if appraisal.event_ref in seen:
                continue
            seen.add(appraisal.event_ref)
            elapsed = (stamp - clock).total_seconds() if clock else 0
            affect = regress_pad(affect, cfg.baseline.vector(), elapsed, cfg.emotion_half_life_seconds)
            mood = regress_pad(mood, cfg.baseline.vector(), elapsed, cfg.mood_half_life_seconds, cfg.inertia)
            affect = lerp_pad(affect, appraisal.target.vector(), cfg.emotion_alpha * appraisal.confidence)
            mood = update_mood(mood, appraisal.target.vector(), cfg.mood_alpha * appraisal.confidence, cfg.inertia)
            clock, latest = stamp, (appraisal, record)
            used.append(record.ref)
        elapsed = (now - clock).total_seconds() if clock else 0
        affect = regress_pad(affect, cfg.baseline.vector(), elapsed, cfg.emotion_half_life_seconds)
        mood = regress_pad(mood, cfg.baseline.vector(), elapsed, cfg.mood_half_life_seconds, cfg.inertia)
        concerns = [r.ref for r in self.store.list_records("temporary_state", snapshot=snapshot)
                    if r.payload.get("kind_hint") == "emotional_echo" and r.payload.get("status") in {"open", "paused"}]
        return {"enabled": cfg.enabled, "algorithm": "emotion-v1", "snapshot": snapshot or self.store.snapshot(),
                "evaluated_at": now.isoformat(), "affect": PAD.from_vector(affect).model_dump(),
                "mood": PAD.from_vector(mood).model_dump(), "baseline": cfg.baseline.model_dump(),
                "last_observed_at": clock.isoformat() if clock else None,
                "appraisal_refs": used, "unique_event_count": len(seen), "omitted_future": omitted_future,
                "retracted_refs": sorted(retracted),
                "open_concern_refs": concerns if cfg.enabled else [],
                "config_fingerprint": digest(cfg.model_dump(mode="json")),
                "last_appraisal": ({"ref": latest[1].ref, "event_ref": latest[0].event_ref,
                                    "label": latest[0].label, "meaning": latest[0].meaning} if latest else None),
                "limitations": ["工程状态模拟；数值并非心理测量。评价是角色的暂定理解，不是用户心理事实。",
                                "未评价的事件不表示无情绪；时间回归不结清关切，也不改变人格。"]}

    def propose_retraction(self, appraisal_ref, reason):
        """Human correction: exclude one impulse, atomically retire its open echo.

        History stays readable. A previously reviewed persona change is a
        separate object and is never silently rolled back by this operation.
        """
        from myatri.persona.temporary import resolve
        snapshot = self.store.snapshot()
        appraisal = resolve(self.store, appraisal_ref, snapshot)
        if appraisal.record_kind != "emotion_appraisal":
            raise ValueError("emotion retraction requires an emotion_appraisal")
        record_id = "emotion_retract_" + digest(appraisal.ref)[:24]
        try:
            self.store.get_record(record_id)
            return None
        except KeyError:
            pass
        from myatri.contracts.emotion import EmotionRetraction
        value = EmotionRetraction(appraisal_ref=appraisal.ref, reason=reason)
        row = Record(record_id=record_id, record_kind="emotion_retraction", agent_id=self.store.agent_id,
            user_id=self.store.user_id, author_kind="human", source_refs=[appraisal.ref],
            body_markdown=reason, payload=value.model_dump())
        records, reads = [row], {appraisal.record_id: appraisal.revision, record_id: 0}
        try:
            echo = self.store.get_record(appraisal.record_id + "_echo", snapshot=snapshot)
        except KeyError:
            echo = None
        if echo and echo.payload.get("status") in {"open", "paused"}:
            entry = TemporaryEntry.model_validate({**echo.payload, "status": "discarded", "reason": reason})
            records.append(Record(record_id=echo.record_id, revision=echo.revision+1, supersedes_revision=echo.revision,
                record_kind="temporary_state", agent_id=self.store.agent_id, user_id=self.store.user_id,
                author_kind="human", source_refs=[echo.ref, row.ref], body_markdown=entry.text, payload=entry.model_dump()))
            reads[echo.record_id] = echo.revision
        return Proposal(proposal_id=record_id, base_snapshot=snapshot, read_versions=reads, records=records,
                        role="human-emotion-retraction", run_id=record_id)

    def context_block(self, state, *, max_bytes=1400):
        if not state["enabled"] or not state["appraisal_refs"]:
            return ""
        payload = {"affect": state["affect"], "mood": state["mood"],
                   "appraisal_refs": state["appraisal_refs"][-2:], "as_of": state["evaluated_at"],
                   "open_concern_refs": state["open_concern_refs"][-2:]}
        # A historical appraisal label is not an eternal current emotion.
        distance = pad_distance(PAD.model_validate(state["affect"]).vector(), self.config.baseline.vector())
        if distance >= 0.05 and state["last_appraisal"]:
            # 同一次变化只说一次：含义句只在变化发生后的短时间窗口内随上下文提供，
            # 之后情绪数据仍在，但不再反复提醒这一件事。
            observed = state.get("last_observed_at")
            try:
                age = None if not observed else (
                    datetime.fromisoformat(state["evaluated_at"])
                    - datetime.fromisoformat(observed)).total_seconds()
            except ValueError:
                age = None
            if age is not None and age <= MEANING_FRESH_SECONDS:
                payload["last_event_meaning"] = state["last_appraisal"]["meaning"][:120]
        prefix = ("\n情绪连续性（暂定数据，可调整；不改变事实、人格或工具权限，不要求生气或讨好）：\n"
                  "可在语气、用词和节奏上自然体现当前情绪与心境，不直接报数值，不刻意表演情绪。\n"
                  "last_event_meaning 是最近一次心情变化的含义：只在第一次开口时可以自然带出一点，"
                  "同一次变化不要在后续轮次反复提起。\n")
        rendered = json.dumps(payload, ensure_ascii=False)
        if len((prefix + rendered).encode("utf-8")) > max_bytes:
            payload.pop("last_event_meaning", None)
            payload["open_concern_refs"] = []
            rendered = json.dumps(payload, ensure_ascii=False)
        if len((prefix + rendered).encode("utf-8")) > max_bytes:
            return ""
        return prefix + rendered

    def rerank_context(self, records, counter_refs, state, *, snapshot=None):
        """Optional bounded experiment: reorder existing slots, never prune evidence.

        Used only by foreground context. Core recall / persona counter-search
        stay unchanged. First four records and all typed counterevidence stay put.
        """
        cfg = self.config
        if not cfg.enabled or not cfg.mood_recall or not state["appraisal_refs"]:
            return list(records), []
        allowed = set(state["appraisal_refs"])
        tags = {r.payload["event_ref"]: EmotionAppraisal.model_validate(r.payload).target
                for r in self.store.list_records("emotion_appraisal", snapshot=snapshot) if r.ref in allowed}
        slots = [i for i, r in enumerate(records) if i >= 4 and r.ref not in counter_refs and r.ref in tags]
        current = PAD.model_validate(state["mood"]).vector()
        scored = []
        for rank, slot in enumerate(slots):
            record = records[slot]
            similarity = 1 - pad_distance(current, tags[record.ref].vector()) / (6 ** 0.5)
            scored.append((-rank / max(1, len(slots)) + cfg.recall_weight * similarity, slot, record))
        ranked = [r for _, _, r in sorted(scored, key=lambda row: (-row[0], row[1]))]
        output = list(records)
        for slot, record in zip(slots, ranked):
            output[slot] = record
        return output, [r.ref for i, r in enumerate(output) if records[i].ref != r.ref]
