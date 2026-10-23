"""Group short follow-up attention, atomic with transport deduplication."""

from myatri.contracts import digest


def followup(db, scope, key, *, wake, now, ttl=600):
    if wake:
        # 2-4 sustained follow-ups per wake: an @-conversation keeps her engaged
        # for a couple of rounds instead of possibly dropping after one reply.
        rounds = 2 + int(digest(["attention-v1", key])[:8], 16) % 3
        db.execute(
            "INSERT OR REPLACE INTO attention_windows VALUES(?,?,?,?)",
            (scope, rounds, now + ttl, key),
        )
        return {"wake": True, "active": False, "remaining": rounds}
    row = db.execute(
        "SELECT remaining,expires FROM attention_windows WHERE scope=?", (scope,)
    ).fetchone()
    if not row or row[0] <= 0 or row[1] <= now:
        return {"wake": False, "active": False, "remaining": 0}
    db.execute(
        "UPDATE attention_windows SET remaining=remaining-1 WHERE scope=?", (scope,)
    )
    return {"wake": False, "active": True, "remaining": row[0] - 1}
