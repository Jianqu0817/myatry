"""Read-only dashboard projections. Never instantiate a writable archive or index."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from myatri.persona.emotion import EmotionState

BEIJING = timezone(timedelta(hours=8))
PURPOSES = frozenset({"conversation", "group_short_memory", "emotion_batch", "emotion",
    "group_reply_guard", "proactive_speak", "group_memory_support_review", "behavior_attribution",
    "A1", "A1_batch", "A1_support", "context_compaction", "temporary_state_activation",
    "reflection", "persona", "interpretation", "experience", "computer_execution", "computer_report"})
KINDS = ("raw_message", "message", "event", "experience", "interpretation", "reflection",
         "summary", "temporary_state", "persona_version", "persona_change", "emotion_appraisal",
         "emotion_retraction", "cognitive_run")


def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=.3)
    db.execute("PRAGMA query_only=ON")
    return db


def instant(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def tokens(value):
    return value if type(value) is int and value >= 0 else None


def empty_daily(now):
    return {"status": "no_data", "date": now.astimezone(BEIJING).date().isoformat(),
        "timezone": "Asia/Shanghai", "requests": 0, "confirmed_requests": 0,
        "unknown_usage_requests": 0, "failed_requests": 0, "undated_rows": 0,
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
        "cached_input_tokens": None, "reasoning_tokens": None, "by_purpose": {},
        "hourly": [{"hour": hour, "tokens": 0, "requests": 0} for hour in range(24)]}


def daily_usage(path, now):
    result = empty_daily(now)
    if not path.exists():
        return result
    sums = Counter()
    details_seen = set()
    try:
        with closing(connect(path)) as db:
            # No LIMIT: today's total must include every attempt, including tool follow-ups.
            # Select metadata only, never prompts or provider reasoning text.
            for stamp, status, purpose, raw in db.execute("SELECT "
                    "json_extract(payload,'$.recorded_at'),json_extract(payload,'$.status'),"
                    "json_extract(payload,'$.purpose'),json_extract(payload,'$.usage') "
                    "FROM usage WHERE json_valid(payload)"):
                when = instant(stamp)
                if when is None:
                    result["undated_rows"] += 1
                    continue
                if when.astimezone(BEIJING).date().isoformat() != result["date"]:
                    continue
                result["requests"] += 1
                usage = json.loads(raw) if isinstance(raw, str) else {}
                usage = usage if isinstance(usage, dict) else {}
                prompt, completion = tokens(usage.get("prompt_tokens")), tokens(usage.get("completion_tokens"))
                if status not in {None, "received"}:
                    result["failed_requests"] += 1
                if status not in {None, "received"} or prompt is None or completion is None:
                    result["unknown_usage_requests"] += 1
                    continue
                result["confirmed_requests"] += 1
                sums.update(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)
                for key, direct, nested, field in (
                        ("cached_input_tokens", "prompt_cache_hit_tokens", "prompt_tokens_details", "cached_tokens"),
                        ("reasoning_tokens", "reasoning_tokens", "completion_tokens_details", "reasoning_tokens")):
                    sub = usage.get(nested)
                    count = tokens(usage.get(direct, sub.get(field) if isinstance(sub, dict) else None))
                    if count is not None:
                        sums[key] += count
                        details_seen.add(key)
                name = purpose if isinstance(purpose, str) and purpose in PURPOSES else "other"
                part = result["by_purpose"].setdefault(name, {"requests": 0, "tokens": 0})
                part["requests"] += 1
                part["tokens"] += prompt + completion
                hour = result["hourly"][when.astimezone(BEIJING).hour]
                hour["tokens"] += prompt + completion
                hour["requests"] += 1
        result["status"] = "ok"
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            result[key] = sums[key] if result["confirmed_requests"] or not result["requests"] else None
        for key in details_seen:
            result[key] = sums[key]
    except (sqlite3.Error, OSError, ValueError, TypeError):
        result = {**empty_daily(now), "status": "temporarily_unavailable"}
    return result


def merge_daily(rows, now):
    result = empty_daily(now)
    available = [r for r in rows if r["status"] == "ok"]
    result["status"] = ("ok" if len(available) == len(rows) else "partial") if rows else "no_data"
    result["libraries_available"], result["libraries_total"] = len(available), len(rows)
    for key in ("requests", "confirmed_requests", "unknown_usage_requests", "failed_requests", "undated_rows"):
        result[key] = sum(r[key] for r in available)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"):
        known = [r[key] for r in available if r[key] is not None]
        result[key] = sum(known) if known else None
    for r in available:
        for name, part in r["by_purpose"].items():
            dest = result["by_purpose"].setdefault(name, {"requests": 0, "tokens": 0})
            for key in dest:
                dest[key] += part[key]
        for dest, part in zip(result["hourly"], r["hourly"], strict=True):
            dest["tokens"] += part["tokens"]
            dest["requests"] += part["requests"]
    return result


class ProjectionEmotionStore:
    """Small adapter to reuse the installed emotion algorithm without archive recovery/writes."""
    def __init__(self, rows, watermark):
        self.rows, self.watermark = rows, watermark

    def snapshot(self):
        return self.watermark

    def list_records(self, kind, *, snapshot=None):
        return [row for row in self.rows if row.record_kind == kind]


def memory_state(root, config, now):
    result = {"status": "no_data", "sampled_at": now.isoformat(), "counts": None,
        "projection_current": None, "index_current": None, "latest_record_at": None,
        "emotion": {"status": "no_data"}}
    paths = list((root / "domains").glob("*/projection.sqlite3"))
    if not paths:
        return result
    if len(paths) != 1:
        return {**result, "status": "ambiguous_domains"}
    path = paths[0]
    try:
        with closing(connect(path)) as db:
            db.execute("BEGIN")  # Pin counts, watermark and emotion to one projection snapshot.
            mark = db.execute("SELECT value FROM metadata WHERE key='archive_watermark'").fetchone()
            watermark = mark[0] if mark else None
            # Count current logical records, not each historical revision.
            latest = ("WITH latest AS (SELECT r.* FROM records r JOIN "
                "(SELECT record_id,MAX(revision) AS rev FROM records GROUP BY record_id) n "
                "ON r.record_id=n.record_id AND r.revision=n.rev) ")
            counts = dict(db.execute(latest + "SELECT kind,COUNT(*) FROM latest GROUP BY kind"))
            result["counts"] = {kind: counts.get(kind, 0) for kind in KINDS}
            result["record_count"] = sum(counts.values())
            result["latest_record_at"] = db.execute("SELECT MAX(created_at) FROM records").fetchone()[0]
            rows = [SimpleNamespace(ref=ref, record_kind=kind, payload=json.loads(payload))
                for ref, kind, payload in db.execute(latest + "SELECT ref,kind,json_extract(json,'$.payload') FROM latest "
                    "WHERE kind IN ('emotion_appraisal','emotion_retraction','temporary_state')")]
        manifests = sorted((path.parent / "commits").glob("*.json"))
        # Filenames encode sequence-commit_id. Only compare watermarks; no source bodies read.
        head = manifests[-1].stem.split("-", 1)[1] if manifests else None
        result["projection_current"] = watermark == head if watermark and head else None
        pointer_path = path.parent / "indexes/current.json"
        if pointer_path.exists():
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            result["index_current"] = pointer.get("watermark") == head if head else None
            result["index_present"] = bool(pointer.get("generation"))
        else:
            result["index_present"] = False
        result["status"] = "ok"
        try:
            state = EmotionState(ProjectionEmotionStore(rows, watermark), config.get("emotion", {})).read(now=now)
            result["emotion"] = {key: state[key] for key in (
                "enabled", "evaluated_at", "affect", "mood", "baseline", "last_observed_at", "unique_event_count", "omitted_future")}
            result["emotion"].update(status="ok" if state["appraisal_refs"] else "no_appraisals",
                retracted_count=len(state["retracted_refs"]), open_concerns=len(state["open_concern_refs"]))
        except (ValueError, TypeError, KeyError, AttributeError):
            result["emotion"] = {"status": "temporarily_unavailable"}
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError):
        result["status"] = "temporarily_unavailable"
    return result


def job_state(path, now=None):
    result = {"status": "no_data", "counts": {}, "expired_leases": None}
    if not path.exists():
        return result
    try:
        with closing(connect(path)) as db:
            db.execute("BEGIN")
            counts = dict(db.execute("SELECT status,COUNT(*) FROM jobs WHERE role='maintenance' GROUP BY status"))
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "lease_until" in columns:
                stamp = (now or datetime.now(timezone.utc)).timestamp()
                result["expired_leases"] = db.execute("SELECT COUNT(*) FROM jobs WHERE role='maintenance' "
                    "AND status='running' AND (lease_until IS NULL OR lease_until<=?)", (stamp,)).fetchone()[0]
        result.update(status="ok", counts={key: counts.get(key, 0)
            for key in ("pending", "running", "succeeded", "failed", "retry_wait", "deferred", "proposed")})
    except (sqlite3.Error, OSError):
        result["status"] = "temporarily_unavailable"
    return result
