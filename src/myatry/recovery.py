"""One versioned maintenance repair per job, using the owning queue's atomic claim."""
import time

VERSION = "bridge-bounded-memory-v1"
# Only failures addressed by the bridge adapters or structured-output transport are eligible.
REPAIRABLE = (
    "association outside bounded evidence candidates", "group event merge cannot change member ownership",
    "group event merge requires matching prior attribution ownership", "batch A1 quote outside the named original window",
    "invalid structured result; no changes submitted", "provider transport failed", "incomplete response: length",
)


class AutoRecovery:
    def __init__(self, app):
        self.app = app

    def tick(self, *, now=None):
        now = time.time() if now is None else now
        queue = self.app.queue
        jobs = queue.list_jobs()
        # Never interfere with a running/queued batch or active foreground conversation.
        if self.app.conversation._lock.locked() or self.app.maintenance.lock.locked():
            return "busy"
        if any(j["role"] == "maintenance" and j["status"] not in {"failed", "succeeded", "cancelled"} for j in jobs):
            return "busy"
        previous = queue.get_meta("bridge_recovery_last_scheduled", 0)
        if now - previous < 300:
            return "backoff"
        for job in jobs:
            if (job["role"] != "maintenance" or job["status"] != "failed"
                    or not any(code in (job.get("error") or "") for code in REPAIRABLE)
                    or now < job["available"] + 60):
                continue
            # Marker and retry_wait are one transaction; restarts cannot grant a new retry.
            if queue.retry_failed_once(job["id"], VERSION, now=now):
                queue.set_meta("bridge_recovery_last_scheduled", now)
                return "scheduled"
        return "idle"
