"""Durable DB-backed job queue + worker (Decision #3).

Claims are atomic (conditional UPDATE), failures retry with exponential
backoff up to max_attempts then go to 'dead' (dead-letter, inspectable via
API). Handlers are idempotent-by-key. Works identically on SQLite and
Postgres; no Redis required. Swap-in point for Celery/RQ later.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .models import AuditLog, Job, utcnow

HANDLERS: dict[str, Callable] = {}


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(session, kind: str, params: dict, idempotency_key: str | None = None) -> Job:
    j = Job(kind=kind, params=params or {}, idempotency_key=idempotency_key)
    try:
        # SAVEPOINT, not a full rollback: on a duplicate idempotency key only
        # this insert is undone — the caller's staged transaction survives.
        with session.begin_nested():
            session.add(j)
            session.flush()
    except IntegrityError:      # duplicate idempotency key -> return existing
        if j in session:
            session.expunge(j)
        return session.scalars(select(Job).where(Job.idempotency_key == idempotency_key)).one()
    audit_detail = {"params": str(params)[:2000]} if params else {}
    session.add(AuditLog(actor="api", action="job.enqueue",
                         subject=f"job:{kind}", detail=audit_detail))
    session.commit()
    return j


def _claim_one(worker_id: str) -> Job | None:
    with SessionLocal() as s:
        rows = s.scalars(select(Job).where(Job.status == "queued", Job.run_after <= utcnow())
                         .order_by(Job.id).limit(10)).all()
        for cand in rows:
            r = s.execute(update(Job).where(Job.id == cand.id, Job.status == "queued")
                          .values(status="running", locked_by=worker_id,
                                  attempts=Job.attempts + 1))
            if r.rowcount == 1:
                s.commit()
                s.refresh(cand)
                return cand
            s.rollback()
    return None


def _run_job(job: Job) -> None:
    fn = HANDLERS.get(job.kind)
    with SessionLocal() as s:
        fresh = s.get(Job, job.id)
        if fn is None:
            fresh.status, fresh.error, fresh.finished_at = "dead", f"no handler for {job.kind}", utcnow()
            s.commit()
            return
        try:
            result = fn(s, fresh) or {}
        except Exception as e:
            s.rollback()
            fresh = s.get(Job, job.id)
            fresh.error = f"{type(e).__name__}: {e}"[:2000]
            if fresh.attempts >= fresh.max_attempts:
                fresh.status, fresh.finished_at = "dead", utcnow()
            else:
                fresh.status = "queued"
                fresh.run_after = utcnow() + timedelta(seconds=min(600, 5 * 2 ** fresh.attempts))
            tb_tail = traceback.format_exc()[-1500:]
            s.add(AuditLog(actor="worker", action="job.failed", subject=f"job:{fresh.id}",
                           detail={"error": fresh.error, "attempts": fresh.attempts,
                                   "status": fresh.status, "traceback": tb_tail}))
            s.commit()
            return
        # success bookkeeping is SEPARATE from the handler try: a serialization
        # or commit hiccup here must not requeue work that already succeeded
        try:
            safe_result = json.loads(json.dumps(result, default=str))
        except Exception:  # noqa: BLE001 — unserializable result → text summary
            safe_result = {"summary": str(result)[:500]}
        try:
            fresh = s.get(Job, job.id)
            fresh.status, fresh.result, fresh.finished_at = "done", safe_result, utcnow()
            s.add(AuditLog(actor="worker", action="job.done", subject=f"job:{fresh.id}",
                           detail={"kind": fresh.kind, "summary": str(result)[:500]}))
            s.commit()
        except Exception:  # noqa: BLE001 — handler work committed; record honestly
            s.rollback()
            traceback.print_exc()


class Worker:
    def __init__(self, threads: int = 2, poll_seconds: float = 2.0):
        self.threads_n = threads
        self.poll = poll_seconds
        self._stop = threading.Event()
        self._ts: list[threading.Thread] = []
        self.worker_id = f"w-{uuid.uuid4().hex[:8]}"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = _claim_one(self.worker_id)
                if job is None:
                    self.heartbeat()
                    self._stop.wait(self.poll)
                    continue
                _run_job(job)
                self.heartbeat()
            except Exception:  # noqa: BLE001 — a DB hiccup must not kill the thread
                traceback.print_exc()
                self._stop.wait(self.poll * 2)

    def _reclaim_stale(self) -> int:
        # This process owns the worker pool: any job still 'running' at startup
        # belongs to a dead worker (crash/kill) — reclaim it so it retries or
        # retires via attempts instead of being stuck forever. But if another
        # worker heartbeated seconds ago, 'running' rows are ALIVE there:
        # re-queuing would double-run jobs against the same DB (corruption).
        from .models import KV
        with SessionLocal() as s:
            hb = s.get(KV, "worker")
            if hb is not None:
                try:
                    ts = (hb.value or {}).get("ts")
                    age = (utcnow() - datetime.fromisoformat(ts)).total_seconds()
                    if age < 30:
                        s.add(AuditLog(actor="worker", action="job.reclaim_skipped",
                                       subject="startup",
                                       detail={"heartbeat_age_s": round(age, 1),
                                               "reason": "live worker detected — "
                                                         "refusing to re-queue its jobs"}))
                        s.commit()
                        return 0
                except (ValueError, TypeError):
                    pass  # unparseable heartbeat: treat as stale
            n = s.execute(update(Job).where(Job.status == "running")
                          .values(status="queued", locked_by=None)).rowcount
            if n:
                s.add(AuditLog(actor="worker", action="job.reclaim", subject="startup",
                               detail={"requeued": n}))
                s.commit()
        return n

    def start(self) -> None:
        self._reclaim_stale()
        for i in range(self.threads_n):
            t = threading.Thread(target=self._loop, name=f"pickemup-worker-{i}", daemon=True)
            t.start()
            self._ts.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._ts:
            # give in-flight handlers room to finish their transaction; daemon
            # exit mid-commit is recovered by _reclaim_stale on next startup
            t.join(timeout=20)

    def heartbeat(self) -> None:
        from .models import KV
        with SessionLocal() as s:
            kv = s.get(KV, "worker")
            val = {"worker_id": self.worker_id, "ts": utcnow().isoformat()}
            if kv:
                kv.value = val
            else:
                s.add(KV(key="worker", value=val))
            s.commit()
