from __future__ import annotations

import asyncio
import json

from myatri.contracts import JobSpec, uid
from myatri.providers import BudgetExceeded
from .jobs import LostLease


class Worker:
    def __init__(self, queue, handlers, apply_result=None, *, lease_seconds=120, max_attempts=3):
        self.queue, self.handlers, self.apply_result = queue, handlers, apply_result
        self.owner, self.lease_seconds, self.max_attempts = uid("worker"), lease_seconds, max_attempts
        self.stopping = False
        self.batch_policy = False
        self.poll_seconds = 5

    async def run_once(self):
        job = self.queue.lease_job(self.owner, lease_seconds=self.lease_seconds)
        if not job:
            return False
        spec = JobSpec.model_validate_json(job["payload"])
        if self.batch_policy and job["role"] not in {"maintenance", "refresh_index"} and not spec.payload.get("manual"):
            self.queue.finish(job["id"], self.owner, "cancelled", "superseded by turn-based maintenance; original messages retained")
            return True
        async def heartbeat():
            while True:
                await asyncio.sleep(self.lease_seconds / 3)
                self.queue.renew(job["id"], self.owner, self.lease_seconds)
        pulse = asyncio.create_task(heartbeat())
        try:
            if job["result"] is not None:
                result = json.loads(job["result"])
            else:
                result = await self.handlers[job["role"]](spec)
                self.queue.save_result(job["id"], self.owner, result)
            # Lease is checked again immediately before publishing saved results.
            self.queue.renew(job["id"], self.owner, self.lease_seconds)
            if self.apply_result:
                if spec.payload.get("manual"):
                    result = {**result, "manual": True}
                await self.apply_result(result)
            self.queue.finish(job["id"], self.owner)
        except BudgetExceeded as exc:
            reason = getattr(exc, "reason", "daily_limit")
            if reason == "ingest_pending":
                self.queue.finish(job["id"], self.owner, "deferred", str(exc), delay=0)
            elif reason == "request_size":
                # The same oversized request does not become smaller tomorrow.
                self.queue.finish(job["id"], self.owner, "failed", "material planning required: " + str(exc))
            else:
                self.queue.finish(job["id"], self.owner, "deferred", str(exc), delay=3600)
        except LostLease:
            pass  # New owner will replay durable result using the same proposal ID.
        except Exception as exc:
            status = "failed" if job["attempts"] >= self.max_attempts else "retry_wait"
            self.queue.finish(job["id"], self.owner, status, type(exc).__name__ + ": " + str(exc)[:300], delay=2**job["attempts"])
        finally:
            pulse.cancel()
            try:
                await pulse
            except (asyncio.CancelledError, LostLease):
                pass
        return True

    async def run(self):
        while not self.stopping:
            if not await self.run_once():
                await asyncio.sleep(self.poll_seconds)
