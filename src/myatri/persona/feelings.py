"""Recent feelings are a view, not a second writable store."""
from .temporary import TemporaryStates


def recent_feelings(store, snapshot=None):
    return [r for r in TemporaryStates(store).list(snapshot=snapshot) if r.payload["kind_hint"] in {"feeling", "emotional_echo"}]


def apply_recent_feeling(store, entry, **kwargs):
    return TemporaryStates(store).create(entry, **kwargs)


def expire_or_revise_feeling(store, entry_id, **kwargs):
    return TemporaryStates(store).review(entry_id, **kwargs)
