"""Local context projections and content-free telemetry; no model calls."""
import json
import re


def excerpt(text, query, limit):
    """Return one literal contiguous passage, including its exact coverage."""
    start = 0
    if len(text) > limit:
        words = re.findall(r"[\w\u4e00-\u9fff]+", query.lower())
        needles = [word for word in words if len(word) >= 2]
        compact = "".join(words)
        needles += [compact[i:i + width] for width in range(min(8, len(compact)), 1, -1)
                    for i in range(min(80, max(0, len(compact) - width + 1)))]
        lower = text.lower()
        # Prefer the longest literal matching phrase. A generic early "我记"
        # must not beat a later "书店约定" and hide the requested detail again.
        hits = [(len(word), lower.find(word)) for word in needles if lower.find(word) >= 0]
        if hits:
            _, position = max(hits, key=lambda hit: (hit[0], -hit[1]))
            start = max(0, position - limit // 3)
        start = min(start, len(text) - limit)
    end = min(len(text), start + limit)
    return {"text": text[start:end], "excerpt_start": start, "excerpt_end": end,
            "omitted_chars": len(text) - (end - start), "full_record_in_context": start == 0 and end == len(text)}


def composition(messages):
    """Byte estimates are deliberately separate from provider-reported tokens."""
    parts = {}
    for message in messages:
        content = message.get("content", "") or ""
        role = message.get("role", "system")
        category = "rules"
        if role in {"user", "assistant"}:
            category = "dialogue"
        elif content.startswith("回忆材料"):
            category = "recall"
        elif content.startswith(("会话语义摘要", "会话来源摘录")):
            category = "summary"
        elif content.startswith(("用户授权迁入", "继承历史档案")):
            category = "imported_history"
        elif content.startswith("当前暂定状态"):
            category = "state"
        elif content.startswith("群聊渠道规则"):
            category = "group_policy_and_summary"
        row = parts.setdefault(category, {"messages": 0, "chars": 0, "estimated_tokens": 0})
        row["messages"] += 1
        row["chars"] += len(content)
        row["estimated_tokens"] += len(json.dumps(message, ensure_ascii=False).encode("utf-8")) + 32
    return parts


def context_stats(messages, **counts):
    return {"version": 1, "measurement": "local_utf8_upper_estimate_not_api_tokens",
            "components": composition(messages), **counts}
