"""Bridge maintenance composition, frozen core 0.2.8 run() with local agent factories.

The orchestration below is copied unchanged from the installed core; only the
ExperienceOrganizer and AssociationAgent dependencies are bridge adapters.
Upstream source SHA256: 92aeb1ddc6507e0fe1feaecd377002198058238c611af4e53b85b386bf14c553
"""
from myatri.runtime.maintenance import Maintenance
from myatri.contracts import JobSpec, Proposal, digest
from myatri.cognition.emotion import EmotionAgent
from myatri.cognition.reflection import ReflectionAgent
from myatri.cognition.temporary_state import TemporaryStateAgent
from myatri.cognition.attribution import AttributionAgent
from myatri.cognition.theme import ThemeAgent
from .memory_repair import ExperienceOrganizer, AssociationAgent


class BridgeMaintenance(Maintenance):
    async def run(self, refs, *, reason="turns"):
        async with self.lock:
            users, raw = self.pending()
            allowed = set(refs)
            # Explicit operator retry can resume a deferred cutoff even after
            # newer batches advanced the ordinary scheduling cursor.
            deferred_refs = self._deferred_refs().intersection(allowed)
            pending_refs = {m.ref for m in users}
            users = [r for r in raw if r.payload.get("role") == "user"
                     and (r.ref in pending_refs or r.ref in deferred_refs)]
            messages = [r for r in users if r.ref in allowed]
            pending_deep = self.app.queue.get_meta("deep_pending_messages", [])
            pending_persona = self.app.queue.get_meta("persona_pending_messages", [])
            forced = reason == "before_compaction"
            if not messages and not (forced and (pending_deep or pending_persona)):
                return {"kind": "noop", "maintenance": "already covered"}
            key = "maintenance_" + digest([r.ref for r in messages] if messages else [reason, pending_deep, pending_persona])[:24]
            progress = self.app.queue.get_meta(key, {})
            if "plan" not in progress:
                number = self.app.queue.get_meta("maintenance_batch_count", 0) + bool(messages)
                progress["plan"] = {"number": number,
                    "deep_messages": list(dict.fromkeys([*pending_deep, *[r.ref for r in messages]])),
                    "persona_messages": list(dict.fromkeys([*pending_persona, *[r.ref for r in messages]])),
                    "deep_due": forced or number % self.deep_every == 0,
                    "persona_due": forced or number % self.persona_every == 0}
            if forced:
                progress["plan"].update(deep_due=True, persona_due=True)
            plan = progress["plan"]
            self.app.queue.set_meta(key, progress)

            async def stage(name, operation):
                if not progress.get(name):
                    await operation()
                    progress[name] = True
                    self.app.queue.set_meta(key, progress)

            commit = self.app.committer.submit_proposal
            if messages:
                await stage("memory", lambda: ExperienceOrganizer(self.app.store, self.app.gateway).organize_batch(messages, commit))
                # Project only known runs from this cutoff, never scan the
                # archive. Assign by immutable run ref so replays do not add.
                quarantine_counts = self.app.queue.get_meta("a1_quarantine_counts", {})
                for message in messages:
                    run_id = "run_exp_" + digest({"message": message.ref, "version": "v1"})[:24]
                    try:
                        run = self.app.store.get_record(run_id)
                    except KeyError:
                        continue
                    count = run.payload.get("source_coverage", {}).get("quarantined_candidates", 0)
                    if type(count) is int and count > 0:
                        quarantine_counts[run.ref] = count
                if quarantine_counts:
                    self.app.queue.set_meta("a1_quarantine_counts", quarantine_counts)
            message_refs = {r.ref for r in messages}
            events = [r for r in self.app.store.list_records("event") if message_refs.intersection(r.source_refs)]
            event_refs = [r.ref for r in events]
            all_events = self.app.store.list_records("event")
            deep_events = [r.ref for r in all_events if set(plan["deep_messages"]).intersection(r.source_refs)]
            persona_events = [r.ref for r in all_events if set(plan["persona_messages"]).intersection(r.source_refs)]
            if plan["deep_due"]:
                await stage("initial_index", self.app.index.update)
            for start in range(0, len(events), 6):
                async def appraise(group=event_refs[start:start + 6]):
                    proposal = await EmotionAgent(self.app.store, self.app.gateway, self.app.emotions.config).propose_batch(group)
                    if proposal:
                        commit(proposal)
                await stage(f"emotion_{start}", appraise)

            def state_refs():
                return [r.ref for r in self.app.states.list() if r.payload["status"] in {"open", "paused"}]

            if event_refs or deep_events or persona_events or state_refs():
                async def association():
                    commit(await AssociationAgent(self.app.store, self.app.gateway, self.app.index).propose(deep_events))
                if plan["deep_due"] and deep_events and any(e["type"] == "similar" and (e["from_ref"] in deep_events or e["to_ref"] in deep_events)
                                      for e in self.app.index.graph.edges):
                    await stage("association", association)

                themes = [(tid, theme) for tid, theme in self.app.index.themes.items() if theme["needs_summary"]][:2]
                for tid, theme in themes if plan["deep_due"] else []:
                    async def summarize_theme(theme_id=tid):
                        proposal = await ThemeAgent(self.app.store, self.app.gateway, self.app.index).propose(theme_id)
                        if proposal:
                            commit(proposal)
                    await stage("theme_" + digest([tid, theme["material_hash"]])[:16], summarize_theme)

                async def reflect():
                    commit(await ReflectionAgent(self.app.store, self.app.gateway).propose([*deep_events, *state_refs()]))
                if plan["deep_due"]:
                    await stage("reflection", reflect)

                async def temporary():
                    relevant = deep_events if forced else event_refs
                    reflections = [r.ref for r in self.app.store.list_records("reflection") if relevant and set(relevant).intersection(r.source_refs)]
                    commit(await TemporaryStateAgent(self.app.store, self.app.gateway).propose([*relevant, *reflections[-2:], *state_refs()]))
                if messages or forced:
                    await stage("temporary", temporary)

                async def attribution():
                    answers = [r.ref for r in raw if r.payload.get("role") == "assistant" and r.payload.get("reply_to") in plan["deep_messages"]]
                    if answers and state_refs():
                        proposal = await AttributionAgent(self.app.store, self.app.gateway).propose([*answers, *deep_events, *state_refs()])
                        self.app._submit_attribution(proposal)
                if plan["deep_due"]:
                    await stage("attribution", attribution)

                async def persona():
                    result = await self.app._persona_revision(JobSpec(role="persona_revision", input_refs=persona_events))
                    if result["kind"] == "revision_candidate":
                        commit(Proposal.model_validate(result["proposal"]))
                        if result["data"]["decision"] == "propose":
                            patch = result["data"]["patch"]
                            reviewed = await self.app._persona_review(JobSpec(role="persona_review", payload={"patch": patch}))
                            await self.app._apply(reviewed)
                if plan["persona_due"]:
                    await stage("persona", persona)

            await stage("index", self.app.index.update)
            # The cursor advances only after memory, state, emotion and review
            # dependencies completed or explicitly deferred with durable runs.
            self.app.store.flush_projection()
            positions = {r.ref: i for i, r in enumerate(raw) if r.payload.get("role") == "user"}
            old_cursor = self.app.queue.get_meta("maintenance_cursor")
            cursor = max([r.ref for r in messages] + ([old_cursor] if old_cursor else []),
                         key=lambda ref: positions[ref], default=None)
            deferred = self.deferred_batches()
            for item in deferred.values():
                if item["status"] == "deferred" and set(item["input_refs"]).issubset(message_refs):
                    item["status"] = "recovered"
            def pending_after(key, planned, completed):
                current = self.app.queue.get_meta(key, [])
                return ([ref for ref in current if ref not in set(planned)] if completed
                        else list(dict.fromkeys([*current, *planned])))
            self.app.queue.set_meta_many({
                "maintenance_cursor": cursor,
                "maintenance_batch_count": max(plan["number"], self.app.queue.get_meta("maintenance_batch_count", 0)),
                "maintenance_deferred_batches": deferred,
                "deep_pending_messages": pending_after("deep_pending_messages", plan["deep_messages"], plan["deep_due"]),
                "persona_pending_messages": pending_after("persona_pending_messages", plan["persona_messages"], plan["persona_due"]),
                "last_maintenance": {"reason": reason, "message_refs": [r.ref for r in messages],
                                     "event_refs": event_refs, "stages": progress}})
            return {"kind": "maintenance_done", "message_refs": [r.ref for r in messages]}
