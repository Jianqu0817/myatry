import json
import math
from collections import Counter
from myatri.contracts import digest

from myatri.providers import BudgetExceeded


class TokenBudget:
    """Conservative UTF-8-byte ceiling until actual provider usage calibrates it."""
    def __init__(self, limit=16000, output_reserve=1024, safety=512, *, compact_at=None):
        self.limit, self.output_reserve, self.safety = limit, output_reserve, safety
        self.compact_at = limit if compact_at is None else compact_at
        if not 0 < self.compact_at <= self.limit:
            raise ValueError("compression threshold must be within context limit")
        self.ratio = 1.0
        self.anchor = None

    @staticmethod
    def _parts(messages, tools):
        return [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in messages] + [
            json.dumps({"tools": tools or []}, ensure_ascii=False, sort_keys=True)]

    def estimate(self, messages, tools=None):
        if self.anchor:
            remaining = Counter(self.anchor["parts"])
            extra = 0
            for part in self._parts(messages, tools):
                key = digest(part)
                if remaining[key]:
                    remaining[key] -= 1
                else:
                    extra += len(part.encode("utf-8")) + 32
            # Replaced/removed blocks are not subtracted until new real usage
            # arrives. Changed blocks and new text use the byte upper bound.
            return self.anchor["prompt_tokens"] + extra + 64
        payload = {"messages": messages, "tools": tools or []}
        return math.ceil(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) * self.ratio) + 64

    def fits(self, messages, tools=None):
        return self.estimate(messages, tools) + self.output_reserve + self.safety <= self.limit

    def needs_compaction(self, messages, tools=None):
        return self.estimate(messages, tools) + self.output_reserve + self.safety > self.compact_at

    def require(self, messages, tools=None):
        if not self.fits(messages, tools):
            raise BudgetExceeded("fixed context/request exceeds budget; reduce recall or increase configured context budget")

    def calibrate(self, messages, usage, tools=None):
        actual = usage.get("prompt_tokens")
        raw = len(json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False).encode("utf-8")) + 64
        if actual:
            # Keep margin; never silently use the English chars/4 rule for Chinese.
            self.ratio = max(self.ratio, actual / max(1, raw) * 1.2)
            self.anchor = {"prompt_tokens": actual,
                           "parts": [digest(part) for part in self._parts(messages, tools)]}
