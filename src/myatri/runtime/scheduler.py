from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from myatri.contracts import JobSpec, digest, parse_timestamp


class Scheduler:
    def __init__(self, queue, *, timezone_name="Asia/Shanghai", period_hours=3, night_hour=2, idle_minutes=90):
        self.queue = queue
        self.zone = ZoneInfo(timezone_name)
        self.period = timedelta(hours=period_hours)
        self.night_hour = night_hour
        self.idle = timedelta(minutes=idle_minutes)

    def tick(self, *, now=None, dirty_refs=None, last_activity=None, review_refs=None):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("scheduler clock must be timezone-aware")
        dirty_refs, review_refs = sorted(set(dirty_refs or [])), sorted(set(review_refs or []))
        local = now.astimezone(self.zone)
        last_raw = self.queue.get_meta("last_scheduled")
        last = parse_timestamp(last_raw, default_zone=str(self.zone)) if last_raw else now - self.period
        night = local.replace(hour=self.night_hour, minute=0, second=0, microsecond=0)
        if night > local:
            night -= timedelta(days=1)
        idle = bool(last_activity and now - last_activity >= self.idle)
        activity_key = last_activity.isoformat() if last_activity else None
        idle_edge = idle and self.queue.get_meta("idle_activity_handled") != activity_key
        periodic = now - last >= self.period
        nightly = last.astimezone(self.zone) < night <= local
        review_key = digest({"states": review_refs, "evidence": dirty_refs}) if review_refs else None
        review_changed = review_key and review_key != self.queue.get_meta("last_review_material")
        if not (idle_edge or periodic or nightly or review_changed):
            return []
        jobs = []
        # Missed windows collapse into this one evidence set, no hourly catch-up loop.
        if dirty_refs and (idle_edge or periodic or nightly):
            jobs.append(self.queue.enqueue(JobSpec(role="cycle", input_refs=dirty_refs), now=now.timestamp()))
            self.queue.set_meta("last_trigger", "night" if nightly else "idle" if idle else "periodic")
        if review_refs and review_changed:
            # New evidence permits a new review; clocks alone are never evidence.
            jobs.append(self.queue.enqueue(JobSpec(role="temporary_review", input_refs=[*review_refs, *dirty_refs],
                prompt_version="v2", payload={"review_refs": review_refs}), now=now.timestamp()))
            self.queue.set_meta("last_review_material", review_key)
        if idle_edge:
            self.queue.set_meta("idle_activity_handled", activity_key)
        self.queue.set_meta("last_scheduled", now.isoformat())
        return jobs
