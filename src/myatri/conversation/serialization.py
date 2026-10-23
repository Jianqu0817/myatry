"""Compact model-facing JSON; never rewrite stored records or paged source text."""
import json


def compact_json(value, *, sort_keys=False):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=sort_keys)
