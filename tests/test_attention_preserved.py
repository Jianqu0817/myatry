"""Selected preserved-regression cases from MYATRI-AstrBot 4428069; fixtures only."""
import json

import sqlite3

from myatry.attention import followup

def test_window_is_one_to_four_group_local_and_expires():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE attention_windows(scope TEXT PRIMARY KEY,remaining INTEGER,expires REAL,wake_key TEXT)"
    )
    counts = set()
    for i in range(50):
        result = followup(db, "one", str(i), wake=True, now=0)
        n = result["remaining"]
        counts.add(n)
        assert 1 <= n <= 4
        assert not followup(db, "other", "x", wake=False, now=1)["active"]
        for j in range(n):
            assert followup(db, "one", str(j), wake=False, now=1)["active"]
        assert not followup(db, "one", "end", wake=False, now=1)["active"]
    assert counts == {1, 2, 3, 4}
    followup(db, "one", "wake", wake=True, now=0)
    assert not followup(db, "one", "expired", wake=False, now=601)["active"]
    db.close()
