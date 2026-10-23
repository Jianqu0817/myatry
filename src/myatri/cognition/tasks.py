from __future__ import annotations

import json

from myatri.contracts import ModelRequest, Proposal, Record, digest, uid
from myatri.prompts import load_prompt
from .materials import json_bytes, select_materials


DECISION_SCHEMA = {
    "type": "object", "required": ["decision", "reason"],
    "properties": {"decision": {"enum": ["propose", "defer", "reject"]}, "reason": {"type": "string"},
                   "claims": {"type": "array", "items": {"type": "object", "required": ["text", "evidence_refs"],
                       "properties": {"text": {"type": "string"}, "evidence_refs": {"type": "array", "items": {"type": "string"}}}}},
                   "temporary_entries": {"type": "array", "items": {"type": "object"}},
                   "updates": {"type": "array", "items": {"type": "object"}},
                   "counter_refs": {"type": "array", "items": {"type": "string"}},
                   "uncertainties": {"type": "array", "items": {"type": "string"}}}, "additionalProperties": False}


async def cognitive_call(store, gateway, role, refs, instruction, schema=DECISION_SCHEMA, *, prompt_version="v1",
                         selection=None, required_refs=(), strict_required=False, query=""):
    selection = selection or select_materials(store, refs, query=query, required_refs=required_refs,
                                              strict_required=strict_required)
    snapshot = selection.snapshot
    def make_request():
        return ModelRequest(role=role, purpose=role, prompt_version=f"{role}/{prompt_version}", output_schema=schema,
                            max_tokens=2048,
                            messages=[{"role": "system", "content": load_prompt(role, prompt_version) + "\n" + instruction +
                                       "\n只输出符合 schema 的 JSON。只可引用本次展示的ref；部分摘录/遗漏不能当全量证据。无材料defer，不生成外部活动。"},
                                      {"role": "user", "content": json.dumps({"materials": selection.material,
                                        "selection": selection.metadata()}, ensure_ascii=False)}])
    request = make_request()
    # Schemas/instructions also consume the same budget. Never increase the
    # provider limit to conceal oversized or unbounded fixed instructions.
    while selection.sources and json_bytes(request.model_dump(mode="json")) + request.max_tokens > request.budget_tokens:
        removed = selection.sources.pop()
        selection.material.pop()
        selection.omitted_count += 1
        if removed.ref in required_refs and removed.ref not in selection.missing_required:
            selection.missing_required.append(removed.ref)
        if "请求包含schema/指令的完整预算进一步省略材料。" not in selection.limitations:
            selection.limitations.append("请求包含schema/指令的完整预算进一步省略材料。")
        request = make_request()
    local_reason = ("缺少可用材料。" if not selection.sources else
                    "必需材料未完整进入预算，缩小范围后再审查。" if strict_required and selection.missing_required else
                    "固定提案/指令超过本次请求预算。" if json_bytes(request.model_dump(mode="json")) + request.max_tokens > request.budget_tokens else None)
    if local_reason:
        data = {"decision": "defer", "reason": local_reason}
        for key in schema.get("required", []):
            if key not in data:
                field = schema.get("properties", {}).get(key, {})
                data[key] = [] if field.get("type") == "array" else False if field.get("type") == "boolean" else ""
        provider, model = "local-policy", "bounded-materials-v1"
    else:
        response = await gateway.generate(request)
        data = json.loads(response.content)
        provider, model = response.provider, response.model
    sources = selection.sources
    run_id = uid("run")
    generator = {"provider": provider, "model": model, "prompt_version": f"{role}/{prompt_version}", "run_id": run_id}
    run = Record(record_id=run_id, record_kind="cognitive_run", agent_id=store.agent_id, user_id=store.user_id,
                 source_refs=[r.ref for r in sources], payload={"role": role, "snapshot": snapshot, "decision": data.get("decision"),
                    "reason": data.get("reason", ""), "provider": provider, "model": model,
                    "materials": selection.metadata(),
                    "prompt_version": f"{role}/{prompt_version}"})
    return snapshot, sources, data, generator, run


def proposal_for(snapshot, sources, records, role, run):
    return Proposal(base_snapshot=snapshot, read_versions={r.record_id: r.revision for r in sources},
                    records=records, role=role, run_id=run.record_id)
