"""Bounded, read-only health counters. Never return prompts, IDs or exceptions."""
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

DELIVERY_REASONS = frozenset({"receipt_timeout", "native_rejected", "binding_unavailable",
    "transport_error", "ack_error", "cancelled", "send_error", "process_restart", "unclassified"})
SUMMARY_REASONS = frozenset({"invalid_schema", "quote_not_exact", "quote_source_mismatch", "self_report_subject_mismatch",
    "source_speaker_mismatch", "transient_personal_claim", "unknown_member_id", "item_budget"})

def _connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=.3)
    db.execute("PRAGMA query_only=ON")
    return db


def health_snapshot(root, *, now=None):
    root = Path(root)
    now = time.time() if now is None else now
    result = {"alerts": [], "delivery": {}, "maintenance": {}, "available": True}
    result["summary_quarantine"] = {"total": None, "by_reason": {}}

    def alert(code, severity, count, message):
        result["alerts"].append({"code": code, "severity": severity, "count": count, "message": message})

    short = root / "short-memory.sqlite3"
    if short.exists():
        try:
            with closing(_connect(short)) as db:
                summaries = db.execute("SELECT COUNT(*) FROM listening_summaries").fetchone()[0]
                count, repeated = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(attempts>=3),0) FROM listening_pending").fetchone()
                columns = {row[1] for row in db.execute("PRAGMA table_info(listening_pending)")}
                parked = db.execute("SELECT COUNT(*) FROM listening_pending WHERE needs_attention=1").fetchone()[0] if "needs_attention" in columns else 0
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                rejected = db.execute("SELECT COUNT(*) FROM listening_rejections").fetchone()[0] if "listening_rejections" in tables else 0
                if "listening_rejections" in tables:
                    columns = {row[1] for row in db.execute("PRAGMA table_info(listening_rejections)")}
                    reasons = Counter()
                    if "code" in columns:
                        for code, count in db.execute("SELECT code,COUNT(*) FROM listening_rejections GROUP BY code"):
                            reasons[code if code in SUMMARY_REASONS else "other"] += count
                    result["summary_quarantine"] = {"total": rejected, "by_reason": dict(reasons)}
            if count and not summaries:
                alert("summary_no_success", "error", count, "旁听已有积压，但尚无成功摘要。")
            if repeated:
                alert("summary_repeated_failure", "error", repeated, "摘要窗口已连续尝试至少三次，请检查失败诊断。")
            if parked:
                alert("summary_needs_attention", "error", parked, "摘要修复仍失败，已暂停自动调用；处理原因后可显式重试。")
            if rejected:
                alert("summary_candidates_quarantined", "warning", rejected, "历史摘要候选已隔离保留，未作为事实写入；不自动改身份或引文放行。")
        except sqlite3.Error:
            result["available"] = False

    delivery = root / "deliveries.sqlite3"
    if delivery.exists():
        try:
            with closing(_connect(delivery)) as db:
                counts = dict(db.execute("SELECT status,COUNT(*) FROM deliveries "
                    "WHERE COALESCE(received_at,updated)>=? AND status IN ('sent','unknown') GROUP BY status",
                    (now - 86400,)))
                known = counts.get("sent", 0) + counts.get("unknown", 0)
                rate = counts.get("unknown", 0) / known if known else 0
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                reasons = {}
                if "delivery_events" in tables:
                    totals = Counter()
                    for code, amount in db.execute("SELECT reason_code,COUNT(*) FROM delivery_events "
                            "WHERE created>=? AND status='unknown' GROUP BY reason_code", (now - 86400,)):
                        totals[code if code in DELIVERY_REASONS else "unclassified"] += amount
                    reasons = dict(totals)
                gap = db.execute("SELECT COUNT(*) FROM deliveries d LEFT JOIN seen_messages s ON s.id=d.id "
                    "WHERE s.id IS NULL AND d.status IN ('sent','unknown','superseded')").fetchone()[0]
            result["delivery"] = {"window_seconds": 86400, "terminal_count": known,
                "unknown_count": counts.get("unknown", 0), "unknown_percent": round(rate * 100, 2),
                "unknown_reasons": reasons, "terminal_without_seen": gap,
                "deduplication_guard": "delivery_ledger_checked_before_seen"}
            if known >= 20 and rate > .2:
                alert("delivery_unknown_rate", "warning", counts.get("unknown", 0),
                    "近24小时超过20%的投递结果未确认；这不等于消息均未发出，禁止自动重发。")
        except sqlite3.Error:
            result["available"] = False

    failed, deferred, unconfirmed = 0, 0, 0
    failure_codes, recovery = Counter(), Counter()
    for path in root.glob("*/runtime.sqlite3"):
        try:
            with closing(_connect(path)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "jobs" in tables:
                    failed += db.execute("SELECT COUNT(*) FROM jobs WHERE role='maintenance' AND status='failed'").fetchone()[0]
                    columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
                    if "error" in columns:
                        for error, in db.execute("SELECT error FROM jobs WHERE role='maintenance' AND status='failed'"):
                            text = str(error or "")
                            code = next((code for pattern, code in (
                                ("association outside", "association_bounds"), ("group event merge", "member_ownership"),
                                ("quote outside", "quote_bounds"), ("all A1 candidates", "all_candidates_quarantined"),
                                ("invalid structured", "structured_output"), ("provider transport", "transport"))
                                if pattern in text), "other")
                            failure_codes[code] += 1
                    if {"id", "error"} <= columns and "schedule" in tables:
                        from .recovery import VERSION
                        for status, amount in db.execute("SELECT j.status,COUNT(*) FROM jobs j JOIN schedule s "
                                "ON s.name='job_recovery:'||j.id||? WHERE j.role='maintenance' GROUP BY j.status", (":"+VERSION,)):
                            recovery[status if status in {"failed", "succeeded", "retry_wait", "running", "deferred"} else "other"] += amount
                if "schedule" in tables:
                    for value, in db.execute("SELECT value FROM schedule WHERE name='maintenance_deferred_batches'"):
                        try:
                            batches = json.loads(value)
                            deferred += sum(isinstance(item, dict) and item.get("status") == "deferred"
                                for item in batches.values()) if isinstance(batches, dict) else 0
                        except (ValueError, TypeError):
                            result["available"] = False
                    for value, in db.execute("SELECT value FROM schedule WHERE name='a1_quarantine_counts'"):
                        try:
                            counts = json.loads(value)
                            if isinstance(counts, dict):
                                unconfirmed += sum(n for n in counts.values() if type(n) is int and n > 0)
                        except (ValueError, TypeError):
                            result["available"] = False
        except sqlite3.Error:
            result["available"] = False
    result["maintenance"] = {"failed_jobs": failed, "deferred_batches": deferred, "unconfirmed_candidates": unconfirmed,
                             "failure_codes": dict(failure_codes), "automatic_recovery": dict(recovery)}
    if failed:
        alert("maintenance_failed", "error", failed, "存在失败维护任务，等待有限恢复或人工处理。")
    if deferred:
        alert("maintenance_deferred", "error", deferred, "有维护批次被隔离，原始资料仍保留，尚未完成记忆整理。")
    if unconfirmed:
        alert("a1_candidates_quarantined", "warning", unconfirmed, "长期提炼中有身份归属不明的候选待核对，合格条目已独立处理。")
    if not result["available"]:
        alert("health_temporarily_unavailable", "warning", 1, "部分健康指标暂时不可读取，不能据此认定系统正常。")
    result["alert_counts"] = dict(Counter(item["severity"] for item in result["alerts"]))
    return result
