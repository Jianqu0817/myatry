from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tomllib

from myatri.contracts import JobSpec, Proposal, digest, parse_timestamp
from myatri.storage import ArchiveStore
from myatri.memory import MemoryIndex
from myatri.retrieval import RecallService
from myatri.providers import DeepSeekProvider, FixtureProvider, Gateway
from myatri.persona.state import PersonaState
from myatri.persona.temporary import TemporaryStates, observed_action_time
from myatri.cognition.experience import ExperienceOrganizer
from myatri.cognition.reflection import ReflectionAgent
from myatri.cognition.temporary_state import TemporaryStateAgent
from myatri.cognition.association import AssociationAgent
from myatri.cognition.theme import ThemeAgent
from myatri.cognition.persona_revision import PersonaRevisionAgent, record_counter_search
from myatri.cognition.attribution import AttributionAgent
from myatri.cognition.emotion import EmotionAgent
from myatri.persona.emotion import EmotionState
from myatri.conversation.engine import Conversation
from myatri.conversation.capture import RequestCapture
from myatri.conversation.session import SessionWindow
from myatri.runtime.commit import Committer
from myatri.runtime.jobs import JobQueue
from myatri.runtime.scheduler import Scheduler
from myatri.runtime.worker import Worker
from myatri.runtime.maintenance import Maintenance


class Application:
    def __init__(self, root="var/atri", agent_id="atri", user_id="local", *, mode="offline",
                 provider=None, embedding_provider=None, config=None):
        self.root = Path(root).resolve()
        self.config = config or {}
        if self.config.get("outreach", {}).get("enabled"):
            raise ValueError("E01 is not implemented; real sending cannot be enabled in V1")
        self.store = ArchiveStore(self.root, agent_id, user_id,
                                  projection_every=self.config.get("storage", {}).get("projection_every_commits", 1))
        self.persona = PersonaState(self.store)
        self.persona.initialize(self.config.get("initial_persona_entries"))
        self.queue = JobQueue(self.root / "runtime.sqlite3", agent_id, user_id)
        if provider is None:
            if mode == "offline":
                provider = FixtureProvider()
            elif mode == "live":
                # Fail closed even if an API key happens to be present.
                chat = self.config.get("chat", {})
                provider = DeepSeekProvider(model=chat.get("model", "deepseek-flash"),
                                            endpoint=chat.get("endpoint", "https://api.deepseek.com"),
                                            allow_paid=chat.get("allow_paid", False),
                                            capabilities_verified=chat.get("capabilities_verified", False))
            else:
                raise ValueError("mode must be offline or live")
        budget = self.config.get("budget", {})
        capture_enabled = self.config.get("context", {}).get("capture_requests", False)
        if type(capture_enabled) is not bool:
            raise ValueError("capture_requests must be a boolean")
        capture = RequestCapture(self.root / "context-debug", provider_name=getattr(provider, "name", ""),
                                 model=getattr(provider, "model", None), metadata_provider=lambda: {
                                     key: value for key, value in self.conversation.context.explain_context().items()
                                     if key in ("context_stats", "history_selection", "summary_omitted_items",
                                                "active_topic", "budget_limit", "compact_at")}) if capture_enabled else None
        self.gateway = Gateway(provider, usage_sink=self.queue.log_usage,
                               budget_ledger=self.queue,
                               daily_token_limit=budget.get("daily_tokens", 200000),
                               input_per_million=budget.get("input_per_million", 0),
                               output_per_million=budget.get("output_per_million", 0), request_observer=capture)
        embedding = self.config.get("embedding") or self.queue.get_meta("embedding", {})
        if embedding_provider is None and (embedding.get("backend") == "bge" or
                (embedding.get("backend") == "auto" and (Path(embedding.get("path", "")) / "manifest.json").is_file())):
            from myatri.providers.local_onnx import LocalOnnxEmbeddingProvider
            embedding_provider = LocalOnnxEmbeddingProvider(Path(embedding["path"]))
        self.index = MemoryIndex(self.store, embedding_provider)
        self.recall = RecallService(self.store, self.index)
        self.states = TemporaryStates(self.store)
        self.emotions = EmotionState(self.store, self.config.get("emotion", {}))
        self.committer = Committer(self.store)
        cognition = self.config.get("cognition", {})
        self.maintenance = Maintenance(self, cognition.get("maintenance_turns", 0),
            deep_every=cognition.get("deep_every_batches", 1), persona_every=cognition.get("persona_every_batches", 1))
        self.conversation = Conversation(self.store, self.gateway, self.recall, self.index,
                                         on_message=self.enqueue_message,
                                         on_answer=self.enqueue_answer,
                                         protocol_store=self.queue,
                                         context_options=self.config.get("context", {}),
                                         enable_tools=mode == "offline" or self.config.get("chat", {}).get("capabilities_verified", False))
        self.conversation.context.emotions = self.emotions
        self.conversation.context.window = SessionWindow(self.store, self.queue)
        if self.maintenance.turns:
            self.conversation.context.compactor.before_compact = self.maintenance.before_compact
        scheduling = self.config.get("schedule", {})
        self.scheduler = Scheduler(self.queue, timezone_name=scheduling.get("timezone", "Asia/Shanghai"),
                                   period_hours=scheduling.get("period_hours", 3),
                                   night_hour=scheduling.get("night_hour", 2), idle_minutes=scheduling.get("idle_minutes", 90))
        self.worker = Worker(self.queue, {"ingest": self._ingest, "index": self._index, "refresh_index": self._refresh_index,
             "cycle": self._cycle, "association": self._association, "theme": self._theme,
             "reflection": self._reflection, "temporary_state": self._temporary,
             "temporary_review": self._temporary, "persona_revision": self._persona_revision,
             "attribution": self._attribution, "emotion": self._emotion,
             "maintenance": self._maintenance,
             "persona_review": self._persona_review}, self._apply)
        self.worker.poll_seconds = cognition.get("poll_seconds", 5)
        if not isinstance(self.worker.poll_seconds, (int, float)) or self.worker.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self.maintenance.turns:
            # Saved per-message jobs from the older policy hand off to the
            # batch cursor. Never replay their saved result after migration.
            self.worker.batch_policy = True

    def clear_context(self):
        """Reset the working window. Caller must hold conversation._lock.

        Long-term recall, persona, emotion, source records and historical
        request captures remain available. No model or external call occurs.
        """
        context = self.conversation.context
        state = context.window.clear()
        context.budget.anchor = None
        context.last_explanation = {}
        return state

    async def _maintenance(self, spec):
        return await self.maintenance.run(spec.input_refs, reason=spec.payload.get("reason", "turns"))

    def enqueue_message(self, message, fixture=None):
        self.queue.set_meta("last_activity", message.created_at)
        if self.maintenance.turns:
            return None
        return self.queue.enqueue(JobSpec(role="ingest", input_refs=[message.ref], priority=50,
                                         payload={"fixture": fixture} if fixture else {}))

    def recover_pending(self):
        if self.maintenance.turns:
            self.maintenance.schedule(force=True)
            return
        # Reconcile persisted source watermark; enqueue is idempotent even after a crash.
        for message in self.store.list_records("raw_message"):
            if message.payload.get("role") == "user":
                self.enqueue_message(message)
            elif message.payload.get("role") == "assistant":
                self.enqueue_answer(message)
        self.enqueue_feedback()

    def enqueue_answer(self, answer):
        if self.maintenance.turns:
            return self.maintenance.schedule(force=True)
        snapshot = answer.payload.get("context_snapshot")
        if not snapshot:
            return None
        states = [r for r in self.store.list_records("temporary_state", snapshot=snapshot) if r.payload["status"] == "open"]
        if not states:
            return None
        # Fixed candidates are part of the durable key. The worker rechecks the
        # answer's historical context, so later thoughts cannot invent actions.
        jobs = [self.queue.enqueue(JobSpec(role="attribution", input_refs=[answer.ref, *[r.ref for r in states[start:start+6]]], priority=35))
                for start in range(0, len(states), 6)]
        return jobs[0] if jobs else None

    def enqueue_feedback(self, event_refs=None):
        from myatri.persona.temporary import resolve
        attempts = []
        for attempt in self.store.list_records("behavior_attempt"):
            if attempt.payload.get("decision_status") not in {"expressed", "attempted"} or not attempt.payload.get("action_refs"):
                continue
            try:
                observed_action_time(self.store, attempt)
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
            attempts.append(attempt)
        if not attempts:
            return
        events = self.store.list_records("event")
        if event_refs is not None:
            events = [r for r in events if r.ref in event_refs]
        for event in events:
            if event.payload.get("reality_scope") not in {"reported", "observed"}:
                continue
            candidates = []
            for attempt in attempts:
                if event.ref in attempt.payload.get("observed_feedback_refs", []):
                    continue
                if any(parse_timestamp(resolve(self.store, s["message_ref"]).created_at) >
                       observed_action_time(self.store, attempt) for s in event.payload.get("source_spans", [])):
                    candidates.append(attempt.ref)
            if candidates:
                for start in range(0, len(candidates), 4):
                    self.queue.enqueue(JobSpec(role="attribution", input_refs=[event.ref, *candidates[start:start+4]], priority=30))

    async def _attribution(self, spec):
        from myatri.persona.temporary import resolve
        refs = []
        for ref in spec.input_refs:
            record = resolve(self.store, ref)
            # Queue keys preserve the triggering evidence. Feedback added by an
            # earlier queued job must be retained when this job is first read.
            refs.append(self.store.get_record(record.record_id).ref if record.record_kind == "behavior_attempt" else ref)
        proposal = await AttributionAgent(self.store, self.gateway).propose(refs)
        return {"kind": "proposal", "proposal": proposal.model_dump(mode="json")}

    def _submit_attribution(self, proposal):
        from myatri.contracts import BehaviorAttempt
        from myatri.storage.archive import ConflictError
        try:
            self.store.get_record(proposal.run_id)
            return  # The run and its behavior records publish atomically.
        except KeyError:
            pass
        original = proposal
        for _ in range(3):
            try:
                return self.committer.submit_proposal(proposal)
            except ConflictError:
                rows = [r for r in original.records if r.record_kind == "cognitive_run"]
                reads = {}
                for candidate in original.records:
                    if candidate.record_kind != "behavior_attempt":
                        continue
                    try:
                        latest = self.store.get_record(candidate.record_id)
                    except KeyError:
                        rows.append(candidate)
                        reads[candidate.record_id] = 0
                        continue
                    for field in ("intent_ref", "action_refs", "occurred_at", "decision_status"):
                        if latest.payload[field] != candidate.payload[field]:
                            raise ValueError("concurrent attribution changed the underlying observed action")
                    combined = list(dict.fromkeys([*latest.payload["observed_feedback_refs"], *candidate.payload["observed_feedback_refs"]]))
                    if combined == latest.payload["observed_feedback_refs"]:
                        continue
                    attempt = BehaviorAttempt.model_validate({**latest.payload, "observed_feedback_refs": combined,
                                                               "outcome_unknown": not combined})
                    row, expected = self.states.prepare_attempt_feedback(attempt, attempt_id=latest.record_id,
                        base_revision=latest.revision, generator=candidate.generator, source_refs=[original.run_id+"@1"])
                    rows.append(row)
                    reads.update(expected)
                # This rebase unions source-checked feedback for the same
                # immutable action, and performs no new semantic/model work.
                proposal = Proposal(proposal_id=original.proposal_id, base_snapshot=self.store.snapshot(),
                    read_versions=reads, records=rows, role=original.role, run_id=original.run_id)
        raise ConflictError("attribution remained concurrent after bounded merge; saved result retained")

    async def _ingest(self, spec):
        from myatri.persona.temporary import resolve
        raw = resolve(self.store, spec.input_refs[0])
        run_id = "run_exp_" + digest({"message": raw.ref, "version": "v1"})[:24]
        try:
            self.store.get_record(run_id)
            return {"kind": "noop"}
        except KeyError:
            pass
        proposal = await ExperienceOrganizer(self.store, self.gateway).propose(raw, fixture=spec.payload.get("fixture"))
        return {"kind": "proposal", "proposal": proposal.model_dump(mode="json"), "next": "index"}

    async def _index(self, spec):
        await self.index.update()
        return {"kind": "indexed", "refs": [r.ref for r in self.store.list_records("event")]}

    async def _refresh_index(self, spec):
        await self.index.update()
        return {"kind": "noop"}

    def _index_refs(self):
        return [r.ref for r in self.store.list_records() if r.record_kind in {
            "event", "interpretation", "reflection", "temporary_state", "persona_change", "theme", "edge", "summary"}]

    async def _cycle(self, spec):
        # Actual model jobs run independently; no database lock spans an API await.
        refs = list(dict.fromkeys([*spec.input_refs, *[r.ref for r in self.states.list() if r.payload["status"] in {"open", "paused"}]]))
        return {"kind": "enqueue", "jobs": [JobSpec(role="association", input_refs=refs).model_dump(mode="json"),
            JobSpec(role="reflection", input_refs=refs).model_dump(mode="json"),
            *[JobSpec(role="theme", input_refs=theme["members"], payload={"theme_id": tid, "hash": theme["material_hash"]}).model_dump(mode="json")
              for tid, theme in self.index.themes.items() if theme["needs_summary"]]]}

    async def _association(self, spec):
        proposal = await AssociationAgent(self.store, self.gateway, self.index).propose(spec.input_refs)
        return {"kind": "proposal", "proposal": proposal.model_dump(mode="json")}

    async def _theme(self, spec):
        theme_id = spec.payload["theme_id"]
        if theme_id not in self.index.themes:
            return {"kind": "noop"}
        proposal = await ThemeAgent(self.store, self.gateway, self.index).propose(theme_id)
        return {"kind": "proposal", "proposal": proposal.model_dump(mode="json")} if proposal else {"kind": "noop"}

    async def _reflection(self, spec):
        proposal = await ReflectionAgent(self.store, self.gateway).propose(spec.input_refs)
        return {"kind": "proposal", "proposal": proposal.model_dump(mode="json"), "next": "temporary_state", "input_refs": spec.input_refs}

    async def _emotion(self, spec):
        proposal = await EmotionAgent(self.store, self.gateway, self.emotions.config).propose(spec.input_refs[0])
        return {"kind": "proposal", "proposal": proposal.model_dump(mode="json")} if proposal else {"kind": "noop"}

    def enqueue_emotion(self, event_ref):
        if not self.emotions.config.enabled or not self.emotions.config.llm_appraisal:
            return None
        return self.queue.enqueue(JobSpec(role="emotion", input_refs=[event_ref], priority=40))

    async def _temporary(self, spec):
        refs = list(dict.fromkeys([*spec.input_refs, *[r.ref for r in self.states.list() if r.payload["status"] in {"open", "paused"}],
                                  *[r.ref for r in self.store.list_records("behavior_attempt")]]))
        proposal = await TemporaryStateAgent(self.store, self.gateway).propose(refs)
        return {"kind": "proposal", "proposal": proposal.model_dump(mode="json"), "next": "persona_revision", "input_refs": spec.input_refs}

    async def _persona_revision(self, spec):
        roots = [r.ref for r in self.store.list_records("event") if r.payload["reality_scope"] in {"reported", "observed"}]
        states = [r.ref for r in self.states.list() if r.payload["status"] in {"open", "paused"}]
        if not roots or not states:
            return {"kind": "noop"}
        # Counter search is a read result with scope/limitations, not another event.
        query = " ".join(self.store.get_record(ref.split("@")[0]).body_markdown for ref in states)
        bundle = self.recall.recall(query, budget=4000)
        search = record_counter_search(self.store, query, bundle)
        refs = [self.persona.read_state().ref, *roots, *states, search.ref,
                *[r.ref for r in self.store.list_records("behavior_attempt")]]
        data, proposal = await PersonaRevisionAgent(self.store, self.gateway).propose(refs)
        return {"kind": "revision_candidate", "proposal": proposal.model_dump(mode="json"), "data": data}

    async def _apply(self, result):
        kind = result["kind"]
        if kind == "maintenance_done":
            self.maintenance.schedule()
            return
        if kind in {"proposal", "revision_candidate"}:
            proposal = Proposal.model_validate(result["proposal"])
            if proposal.role == "attribution":
                self._submit_attribution(proposal)
            else:
                try:
                    self.committer.submit_proposal(proposal)
                except Exception as exc:
                    from myatri.storage import ConflictError
                    if not isinstance(exc, ConflictError) or proposal.role != "emotion":
                        raise
                    appraisal = next((r for r in proposal.records if r.record_kind == "emotion_appraisal"), None)
                    if appraisal is None:
                        raise
                    # A concurrent writer may already have committed the one
                    # canonical event appraisal. Never apply a second impulse.
                    prior = self.store.get_record(appraisal.record_id)
                    if prior.payload["event_ref"] != appraisal.payload["event_ref"]:
                        raise
                    return
            new_events = [r.ref for r in proposal.records if r.record_kind == "event"]
            if new_events:
                self.enqueue_feedback(new_events)
                for event_ref in new_events:
                    self.enqueue_emotion(event_ref)
            if any(r.record_kind == "behavior_attempt" for r in proposal.records):
                self.enqueue_feedback()
                self.queue.enqueue(JobSpec(role="temporary_state", prompt_version="v2", input_refs=
                    [r.ref for r in proposal.records if r.record_kind == "behavior_attempt"]))
            if any(r.record_kind in {"edge", "theme", "temporary_state", "persona_change", "reflection", "behavior_attempt"} for r in proposal.records):
                self.queue.enqueue(JobSpec(role="refresh_index", input_refs=self._index_refs(), priority=20))
            if kind == "revision_candidate" and result["data"]["decision"] == "propose":
                from myatri.contracts import PersonaPatch
                patch = PersonaPatch.model_validate(result["data"]["patch"])
                self.queue.enqueue(JobSpec(role="persona_review", input_refs=patch.evidence_refs,
                                           target_versions={"persona": patch.base_revision}, payload={"patch": patch.model_dump(mode="json")}))
            next_role = result.get("next")
            if next_role:
                refs = list(dict.fromkeys([*result.get("input_refs", []), *[r.ref for r in proposal.records if r.record_kind in {"event", "reflection", "temporary_state"}]]))
                if next_role == "index":
                    refs = self._index_refs()
                self.queue.enqueue(JobSpec(role=next_role, input_refs=refs,
                    payload={"manual": True} if result.get("manual") else {},
                    prompt_version="v2" if next_role == "temporary_state" else "v1"))
        elif kind == "indexed":
            if result["refs"]:
                self.queue.enqueue(JobSpec(role="cycle", input_refs=result["refs"], payload={"manual": True} if result.get("manual") else {}))
        elif kind == "enqueue":
            for spec in result["jobs"]:
                if result.get("manual"):
                    spec = {**spec, "payload": {**spec.get("payload", {}), "manual": True}}
                self.queue.enqueue(JobSpec.model_validate(spec))
        elif kind == "review_result":
            from myatri.contracts import PersonaPatch
            from myatri.persona.patch import PersonaPatcher
            self.committer.submit_proposal(Proposal.model_validate(result["proposal"]))
            patch = PersonaPatch.model_validate(result["patch"])
            if result["review"]["decision"] == "accept":
                # All model work has been durably saved by Worker before this atomic publish.
                try:
                    prepared = PersonaPatcher(self.store).propose(patch, result["review_ref"], consolidate_refs=patch.temporary_refs)
                    # Stable replay key and IDs prevent duplicate persona changes after process exit.
                    stable = "change_" + digest(result["patch"])[:24]
                    mapping = {prepared.records[0].record_id: stable}
                    def replace_refs(value):
                        if isinstance(value, str):
                            for old, new in mapping.items():
                                value = value.replace(old, new)
                            return value
                        if isinstance(value, list):
                            return [replace_refs(v) for v in value]
                        if isinstance(value, dict):
                            return {k: replace_refs(v) for k, v in value.items()}
                        return value
                    from myatri.contracts import Record
                    prepared.records = [Record(**replace_refs(r.model_dump(exclude={"content_hash"}))) for r in prepared.records]
                    prepared.proposal_id = "persona-" + stable
                    self.committer.submit_proposal(prepared)
                    self.queue.enqueue(JobSpec(role="refresh_index", input_refs=self._index_refs(), priority=20))
                except ValueError:
                    # A replay after successful commit is complete only if the exact bound change exists.
                    stable = "change_" + digest(result["patch"])[:24]
                    prior = self.store.get_record(stable)
                    if prior.payload.get("patch") != result["patch"]:
                        raise

    async def _persona_review(self, spec):
        from myatri.contracts import PersonaPatch, Record
        from myatri.cognition.reviewer import Reviewer
        patch = PersonaPatch.model_validate(spec.payload["patch"])
        review, proposal, review_ref = await Reviewer(self.store, self.gateway).review(patch)
        record = proposal.records[0]
        proposal.records = [Record(**{**record.model_dump(exclude={"content_hash"}),
            "payload": {**record.payload, "patch_hash": digest(patch.model_dump(mode="json"))}})]
        return {"kind": "review_result", "proposal": proposal.model_dump(mode="json"), "review": review,
                "review_ref": review_ref, "patch": patch.model_dump(mode="json")}

    async def drain(self, max_jobs=100):
        count = 0
        while count < max_jobs:
            if self.maintenance.turns:
                self.maintenance.schedule(force=True)
            if not await self.worker.run_once():
                break
            count += 1
        return count

    async def run_background(self):
        self.recover_pending()
        worker = asyncio.create_task(self.worker.run())
        try:
            while not self.worker.stopping:
                if worker.done():
                    # Surface a dead worker instead of silently maintaining an
                    # apparently active scheduler with no consumer.
                    await worker
                    raise RuntimeError("background worker exited unexpectedly")
                if self.maintenance.turns:
                    self.maintenance.schedule()
                    await asyncio.sleep(self.worker.poll_seconds)
                    continue
                now = datetime.now(timezone.utc)
                raw_activity = self.queue.get_meta("last_activity")
                try:
                    last = parse_timestamp(raw_activity, default_zone=str(self.scheduler.zone)) if raw_activity else None
                except (ValueError, TypeError):
                    last = None
                events = [r.ref for r in self.store.list_records("event")]
                # Task keys include material refs; unchanged inputs are not repeated.
                review, issues = [], []
                if raw_activity and last is None:
                    issues.append({"field": "last_activity", "error": "invalid timestamp; idle trigger disabled"})
                for state in self.states.list():
                    if state.payload["status"] not in {"open", "paused"} or not state.payload.get("review_at"):
                        continue
                    try:
                        due = parse_timestamp(state.payload["review_at"], default_zone=str(self.scheduler.zone))
                    except (ValueError, TypeError):
                        issues.append({"ref": state.ref, "field": "review_at", "error": "invalid timestamp; isolated from scheduler"})
                        continue
                    if due <= now:
                        review.append(state.ref)
                if self.queue.get_meta("scheduler_issues", []) != issues:
                    self.queue.set_meta("scheduler_issues", issues)
                self.scheduler.tick(now=now, dirty_refs=events, last_activity=last, review_refs=review)
                await asyncio.sleep(1)
        finally:
            self.worker.stopping = True
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass


def load_config(path=None):
    if path is None:
        return {}
    with Path(path).open("rb") as stream:
        return tomllib.load(stream)
