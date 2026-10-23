from myatri.contracts import Record, digest


DEFAULT_ENTRIES = {
    "P01": "我的名字是 ATRI。我会坦诚地区分记忆、推测和未知。",
    "P02": "我珍惜共同经历，也允许彼此保留不同感受。",
    "P03": "分歧时通常先迁就，不主动表达期待。",
}


class PersonaState:
    def __init__(self, store):
        self.store = store

    def initialize(self, entries=None):
        try:
            current = self.read_state()
            if entries is not None and entries != current.payload["entries"]:
                raise ValueError("persona already initialized; use explicit human edit for later changes")
            return current
        except KeyError:
            entries = entries or DEFAULT_ENTRIES
            record = Record(record_id="persona", record_kind="persona_version", agent_id=self.store.agent_id,
                            user_id=self.store.user_id, author_kind="human_import", body_markdown=self.render(entries),
                            payload={"entries": entries, "entry_hashes": {k: digest(v) for k, v in entries.items()}, "origin": "initial_template"})
            self.store.commit([record], idempotency_key="initialize-persona", expected={"persona": 0})
            return record

    def read_state(self, snapshot=None):
        return self.store.get_record("persona", snapshot=snapshot)

    @staticmethod
    def render(entries):
        return "\n\n".join(f"## {key}\n\n{text}" for key, text in entries.items())

    @staticmethod
    def parse_template(text):
        import re
        chunks = re.split(r"(?m)^## ([A-Za-z0-9_-]+)\s*$", text)
        entries = {}
        for index in range(1, len(chunks), 2):
            key, body = chunks[index], chunks[index+1].strip()
            if not body or key in entries:
                raise ValueError("template entries must be nonempty and unique")
            entries[key] = body
        if not entries:
            raise ValueError("template requires stable ## entry_id headings")
        return entries

    def diff(self, before, after):
        a = self.store.get_record("persona", revision=before).payload["entries"]
        b = self.store.get_record("persona", revision=after).payload["entries"]
        return {k: {"before": a.get(k), "after": b.get(k)} for k in a.keys() | b.keys() if a.get(k) != b.get(k)}
