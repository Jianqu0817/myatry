"""Bounded, rebuildable topic hints. Links are attention, never factual evidence."""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime

from myatri.contracts import digest


def terms(text):
    text = re.sub(r"\s+|[，。！？、,.!?…]|小[温溫]暖", "", text.lower())
    return {text[i:i + 2] for i in range(max(0, len(text) - 1))}


def overlap(left, right):
    a, b = terms(left), terms(right)
    return len(a & b) / max(1, min(len(a), len(b)))


class TopicTracker:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS topic_hints(scope TEXT,id TEXT,topic TEXT,sender TEXT,"
                       "stamp REAL,terms TEXT,PRIMARY KEY(scope,id))")

    def add(self, scope, message):
        stamp = datetime.fromisoformat(message["sent_at"].replace("Z", "+00:00")).timestamp()
        words = terms(message["text"])
        with closing(sqlite3.connect(self.path)) as db, db:
            old = db.execute("SELECT topic FROM topic_hints WHERE scope=? AND id=?",
                             (scope, message["message_id"])).fetchone()
            if old:
                return old[0]
            rows = db.execute("SELECT id,topic,sender,stamp,terms FROM topic_hints WHERE scope=? "
                              "AND stamp<=? AND stamp>=? ORDER BY stamp DESC,rowid DESC LIMIT 100",
                              (scope, stamp, stamp - 1800)).fetchall()
            quoted = next((r for r in rows if r[0] == message.get("reply_to_id")), None)
            scored = [(len(words & set(json.loads(r[4]))) / max(1, min(len(words), len(json.loads(r[4])))), r)
                      for r in rows]
            score, best = max(scored, key=lambda pair: pair[0]) if scored else (0, None)
            short = len(message["text"].strip()) <= 12
            own = next((r for r in rows if r[2] == message["sender_id"] and stamp-r[3] <= 120), None)
            if quoted:
                topic = quoted[1]
            elif score >= .22:
                topic = best[1]
            elif short and own:
                topic = own[1]
            elif short and rows and stamp-rows[0][3] <= 120 and len({r[1] for r in rows if stamp-r[3] <= 120}) == 1:
                topic = rows[0][1]
            else:
                topic = "topic_" + digest([scope, message["message_id"]])[:24]
            db.execute("INSERT INTO topic_hints VALUES (?,?,?,?,?,?)", (scope, message["message_id"], topic,
                       message["sender_id"], stamp, json.dumps(sorted(words)[:256], ensure_ascii=False)))
            db.execute("DELETE FROM topic_hints WHERE scope=? AND rowid NOT IN "
                       "(SELECT rowid FROM topic_hints WHERE scope=? ORDER BY stamp DESC,rowid DESC LIMIT 1000)",
                       (scope, scope))
            return topic

    def lookup(self, scope, message_id):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT topic FROM topic_hints WHERE scope=? AND id=?", (scope, message_id)).fetchone()
            return row[0] if row else None

    def describe(self, scope, topic):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT id,sender FROM topic_hints WHERE scope=? AND topic=? "
                              "ORDER BY stamp DESC,rowid DESC LIMIT 30", (scope, topic)).fetchall()
        return {"topic_id": topic, "participants": sorted({r[1] for r in rows}),
                "source_message_ids": [r[0] for r in reversed(rows)],
                "status": "attention_hint_not_verified_fact"}


def select_summaries(summaries, query, sender, *, max_chars=30000):
    """Bound relevance; carry uncertainty/corrections without inventing freshness."""
    ranked = []
    for summary in summaries:
        for item in summary["items"]:
            row = {key: item[key] for key in ("text", "speaker_id", "subject_id", "attribution", "source_message_ids")}
            row.update({key: item[key] for key in ("facet", "change", "supersedes_message_ids", "valid_time", "interaction_kind", "source_quotes") if key in item})
            row.update(window_end=summary["window_end"], created_at=summary["created_at"])
            score = overlap(query, item["text"]) * 5 + (item["subject_id"] == sender)
            score += .5 if any(word in item["text"] for word in ("更正", "不再", "取消", "改为", "约定", "答应")) else 0
            row["attention_score"] = score
            ranked.append((score, summary["window_end"], row))
    ranked.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    selected, used, seen = [], 0, set()
    for _, _, row in ranked:
        key = (row["speaker_id"], row["subject_id"], row["text"])
        size = len(json.dumps(row, ensure_ascii=False))
        if key not in seen and used + size <= max_chars:
            selected.append(row)
            used += size
            seen.add(key)
    return {"items": sorted(selected, key=lambda r: r["window_end"]), "omitted_item_count": len(ranked)-len(selected),
            "note": "按本轮人物和话题选择；未选择不代表遗忘，摘要不是原话。"}
