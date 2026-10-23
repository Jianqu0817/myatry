"""One generation, at most three semantic chat bubbles; no extra model call."""
import re


SEPARATOR = "<|msg|>"
REPLY_LENGTH_RULES = (
    "本轮回复长度：群聊通常20—100字，硬上限160字（标点计入，空白不计）；"
    "不设最低字数，短回应可以更短，不凑字。"
)
BUBBLE_RULES = (
    "\n发送格式（一次生成，桥接按分隔符拆成气泡）：" + REPLY_LENGTH_RULES +
    "总长不超过30字时只写一条；超过30字时，按自然语义优先分成2条，最多3条；"
    "每条以完整小意思为准，通常10—40字，不拆短语、引用或半句话。"
    f"只在消息之间写专用分隔符{SEPARATOR}；没有自然断点时允许一条。"
    "不写编号、JSON或发送说明，分隔符前后不得添加多余开场或收尾；"
    "不要声称自己调用了“多条发送”工具。"
)


def prepare_bubbles(text, policy):
    marked = SEPARATOR in text
    body = policy.prepare(text.replace(SEPARATOR, "\n\n"))
    if not body:
        return body, []
    if "`" in body:
        return body, [body]
    if marked:
        parts = [part.strip() for part in body.split("\n\n") if part.strip()]
    elif '\n' in body and not re.search(r'(?m)^\s*(?:[-*+] |\d+[.)] |\||#{1,6} )', body):
        # Casual Chinese replies often omit punctuation. A deliberate line break
        # still separates bubbles, even when there are no sentence terminators.
        boundaries = [0]
        for match in re.finditer(r'\n+', body):
            prefix = body[:match.start()]
            if any(prefix.count(left) > prefix.count(right) for left, right in
                   (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"))) or prefix.count('"') % 2:
                continue
            boundaries.append(match.end())
        parts = [body[a:b].strip() for a, b in zip(boundaries, [*boundaries[1:], len(body)], strict=True)
                 if body[a:b].strip()]
    elif sum(not c.isspace() for c in body) <= 30 or re.search(r"https?://|www\.", body):
        return body, [body]
    else:
        # Only sentence boundaries are a fallback; never cut words to hit 30.
        boundaries = []
        for match in re.finditer(r'[。！？!?]+[”’」』）)]*', body):
            prefix = body[:match.end()]
            if any(prefix.count(left) > prefix.count(right) for left, right in
                   (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"))) or prefix.count('"') % 2:
                continue
            boundaries.append(match.end())
        starts = [0, *boundaries]
        ends = [*boundaries, len(body)]
        parts = [body[a:b].strip() for a, b in zip(starts, ends, strict=True) if body[a:b].strip()]
    if len(parts) > 3:
        parts = [parts[0], parts[1], "\n\n".join(parts[2:])]
    return body, parts or [body]
