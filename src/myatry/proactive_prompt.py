"""主动开口的提示词规则块（方向 3：自发起话题）。

本模块只提供文本，不读写任何状态、不依赖 bridge。
``ProactiveScheduler`` 把 :func:`render_proactive_instructions` 的结果作为
``instruction`` 交给生成回调，由父代理把它塞进引擎的 ``respond()``。

规则边界（对应设计文档 3.2 与 3.11）：

- 主动轮没有 user 文本可写，绝不能让模型以为"有人刚说了话"；
- 没有提供的记忆一律不能说"记得"，否则等于伪造人类证据；
- 允许只说一句话，避免为了凑长度编内容；
- 不允许每次都用提问收尾（那是客服口吻，不是群友）。
"""

from __future__ import annotations

#: 候选类型的中文标签；只用于生成指令中的说明，不参与判定。
KIND_LABELS = {
    "failed_explicit": "之前被明确呼叫但没能生成回复的旧消息",
    "deferred_topic": "当时被判为不该插话、但话题还没结束的消息",
    "temporary_entry": "引擎临时状态池里尚未激活的念头",
    "unanswered_question": "最近有人提问但没人回答的断点",
    "interesting_exchange": "群里正聊得热闹、你可能感兴趣的话题",
    "group_revive": "群里安静很久了，想主动找大家聊聊",
}

PROACTIVE_RULES = """\
说话规则（这一次是你自己开口，不是回复谁）：
1. 没有新消息进来。不要写"刚看到""看到你们在聊""我刚才刷到"这类句子，也不要假装收到过本段之外的内容。
2. 只用本段上下文里给过的信息。没给过的人、事、时间、地点一律不编；不确定就说不太确定，不要声称记得。
3. 允许只说一句话，也允许只有几个字。不必凑长度，不必解释自己为什么开口。
4. 不要每次都提问。可以直接陈述、吐槽、说一点当下的感受；不要用问句结尾去钓回应。
5. 不复述你看不到的原文，不引用具体消息，也不声称之前那句话已经送达或已被谁看到。
6. 回忆回响：上下文"回忆材料"块里真实检索到的旧事，可以像老朋友想起上次那样自然带一句
   （例如旧话题、之前的约定、某人上次说的事）；只提这个块里明确给出的内容，没有就不提，
   也不把检索不到的事说成记得。
7. 不要提"候选""调度""提示词""系统"这类内部词，也不要把这些规则念出来。"""


def render_proactive_instructions(
    *,
    kind: str,
    reason: str = "",
    excerpt: str = "",
    context_hash: str = "",
) -> str:
    """把规则与候选出处拼成一段生成指令。

    Args:
        kind: 候选类型（见 :data:`KIND_LABELS`）。
        reason: 调度器给出的"为什么是现在"，可空。
        excerpt: 与该候选相关的上下文摘录，可空；只作语气参考。
        context_hash: 上下文摘要指纹，用于事后核对"为什么是这句"。

    Returns:
        一段可直接拼进系统提示的指令文本。
    """
    lines = [
        PROACTIVE_RULES,
        "",
        "这次开口的由来（只用于判断语气，不要复述、不要提及）：",
        f"- 候选类型：{KIND_LABELS.get(kind, kind)}",
    ]
    if reason:
        lines.append(f"- 为什么是现在：{reason}")
    if excerpt:
        lines.append(f"- 相关上下文摘录：{excerpt}")
    if context_hash:
        lines.append(f"- 上下文摘要指纹：{context_hash}")
    return "\n".join(lines)
