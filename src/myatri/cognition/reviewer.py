from .tasks import cognitive_call, proposal_for


REVIEW_SCHEMA = {"type": "object", "required": ["decision", "reason", "scope_ok", "counterexamples_checked"],
    "properties": {"decision": {"enum": ["accept", "revise", "defer", "reject"]}, "reason": {"type": "string", "minLength": 1},
                   "scope_ok": {"type": "boolean"}, "counterexamples_checked": {"type": "boolean"}}, "additionalProperties": False}


class Reviewer:
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway

    async def review(self, patch):
        refs = list(dict.fromkeys([f"persona@{patch.base_revision}", *patch.evidence_refs, *patch.counter_refs,
                                  *patch.temporary_refs, *patch.attempt_refs, *patch.counter_search_refs]))
        snapshot, sources, data, generator, run = await cognitive_call(
            self.store, self.gateway, "reviewer", refs,
            "A7：只读审查以下局部人格提案，检查独立经历根、反例、适用范围与迎合。两个模型同意不构成事实证明。"
            "如仅适合临时状态或依据不足，应defer。\n" + patch.model_dump_json(), REVIEW_SCHEMA,
            required_refs=refs, strict_required=True)
        run.payload["review"] = data
        # Build a new validated record because payload hashes are immutable.
        run = type(run)(**{**run.model_dump(exclude={"content_hash"}), "payload": run.payload})
        return data, proposal_for(snapshot, sources, [run], "A7", run), run.ref
