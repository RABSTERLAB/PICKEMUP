"""Scheduled monitoring loop (directive Phase 5) wired to the reporting layer.

One `monitor_tick` job per interval: re-run the analysis engines over stored
records, diff the outcome against the previous tick (escalations, new
clusters, collection bursts), raise deduplicated alerts, then — if enabled —
produce a report (LLM-narrated when connected, template otherwise) and
deliver it. Delivery policy is deliberate:

  * the OPERATOR always gets the report (user_emails) — required path,
  * external authorities emails only when explicitly enabled AND the report
    contains actionable findings — every send audited.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .jobs import Worker, enqueue, handler
from .llm import get_config
from .models import Alert, AuditLog, DetectionResult, Job, KV, Post, utcnow
from .reporting import generate_report, send_report_email, smtp_cfg
from . import pipeline

MONITOR_KV = "monitor_config"
STATE_KV = "monitor_state"
SCHED_INTERVAL_S = 20

DEFAULTS = {
    "enabled": False,
    "interval_minutes": 30,
    "auto_report": True,
    "email_user": True,
    "email_authorities": False,
    "user_emails": [],
    "authorities_emails": [],
    "language": "en",
    "burst_post_threshold": 150,   # new posts collected within one tick
}


def get_monitor_config(session: Session) -> dict:
    row = session.get(KV, MONITOR_KV)
    cfg = dict(DEFAULTS)
    if row and isinstance(row.value, dict):
        cfg.update(row.value)
    return cfg


def save_monitor_config(session: Session, patch: dict) -> dict:
    row = session.get(KV, MONITOR_KV)
    cfg = dict(row.value) if row and isinstance(row.value, dict) else dict(DEFAULTS)
    for k, v in (patch or {}).items():
        if k not in DEFAULTS:
            continue
        if k in ("user_emails", "authorities_emails") and isinstance(v, list):
            v = [str(e).strip().lower() for e in v if str(e).strip()]
        if k == "interval_minutes":
            v = max(5, min(1440, int(v)))
        cfg[k] = v
    cfg["enabled"] = bool(cfg["enabled"]) and bool(cfg.get("interval_minutes"))
    if row:
        row.value = cfg
    else:
        session.add(KV(key=MONITOR_KV, value=cfg))
    session.commit()
    return cfg


def diff_state(prev: dict, cur: dict) -> list[dict]:
    """Pure diff -> candidate alerts. prev/cur: {verdicts:{id:v}, clusters:{cid:1}, new_posts:n}"""
    found: list[dict] = []
    for aid, v in cur.get("verdicts", {}).items():
        old = prev.get("verdicts", {}).get(aid)
        if v in ("likely_automated", "mixed_automation") and old != v:
            found.append({"kind": "monitor.automation_escalation", "subject": aid,
                          "severity": "high" if v == "likely_automated" else "medium",
                          "evidence": {"from": old or "unscored", "to": v}})
    for cid in cur.get("clusters", {}):
        if cid not in prev.get("clusters", {}):
            found.append({"kind": "monitor.new_cluster", "subject": cid,
                          "severity": "medium", "evidence": {"cluster_id": cid}})
    if cur.get("new_posts", 0) >= cur.get("burst_threshold", 150):
        found.append({"kind": "monitor.collection_burst", "subject": "corpus",
                      "severity": "low",
                      "evidence": {"posts_since_last_tick": cur["new_posts"],
                                   "threshold": cur.get("burst_threshold", 150)}})
    return found


def _current_state(session: Session, since) -> dict:
    from datetime import datetime
    if isinstance(since, str):  # stored as ISO in the KV snapshot; SQLite
        try:                    # DateTime binds reject raw strings
            since = datetime.fromisoformat(since)
        except ValueError:
            since = None
    verdicts = {r.subject_id: r.verdict for r in session.scalars(
        select(DetectionResult).where(DetectionResult.kind == "automation")).all()}
    clusters = {r.subject_id: 1 for r in session.scalars(
        select(DetectionResult).where(DetectionResult.kind == "cluster")).all()}
    new_posts = 0
    if since:  # first tick has no baseline -> no burst claim
        new_posts = session.scalar(select(func.count(Post.id)).where(
            Post.collected_at > since)) or 0
    return {"verdicts": verdicts, "clusters": clusters, "new_posts": new_posts}


@handler("monitor_tick")
def job_monitor_tick(session: Session, job: Job) -> dict:
    cfg = get_monitor_config(session)
    state_row = session.get(KV, STATE_KV)
    prev = dict(state_row.value) if state_row and isinstance(state_row.value, dict) else {}
    last_run = prev.get("last_run")

    # 1) refresh engines over stored records (same handlers the buttons call)
    fake = SimpleNamespace(id=job.id)
    auto = pipeline.job_analyze_automation(session, fake)
    risk = pipeline.job_analyze_content_risk(session, fake)
    coord = pipeline.job_analyze_coordination(session, fake)

    # 1b) refresh the typed entity index + evaluate correlation rules (ported design)
    pipeline.reindex_entities(session)
    from .correlate import run_correlations
    corr = run_correlations(session)

    # 2) diff -> alerts
    cur = _current_state(session, last_run)
    cur["burst_threshold"] = cfg["burst_post_threshold"]
    hits = diff_state(prev, cur)
    tick_stamp = utcnow().strftime("%Y-%m-%dT%H")
    created_alerts = []
    for h in hits:
        # escalation dedupe must carry the NEW verdict or later transitions for
        # the same account are swallowed forever by the unique-key skip
        suffix = tick_stamp if h["kind"] == "monitor.collection_burst" else \
            (h["evidence"].get("to", "") if h["kind"] == "monitor.automation_escalation" else "")
        dedupe = f"{h['kind']}:{h['subject']}:{suffix}"
        exists = session.scalar(select(Alert).where(Alert.dedupe_key == dedupe))
        if exists:
            continue
        session.add(Alert(kind=h["kind"], severity=h["severity"], dedupe_key=dedupe,
                          subject_id=h["subject"], evidence=h["evidence"]))
        created_alerts.append(h)
    session.add(AuditLog(actor="worker", action="monitor.tick", subject=f"job:{job.id}",
                         detail={"analysis": {"automation": auto, "risk": risk, "coordination": coord},
                                 "correlations": corr["raised"],
                                 "findings": len(created_alerts) + corr["raised"]}))

    # 3) report + delivery
    report_id = None
    actionable = any(a["severity"] in ("high", "medium") for a in created_alerts) \
        or corr["raised"] > 0
    if cfg["auto_report"] and (created_alerts or not prev.get("last_report_at")):
        llm_on = get_config(session).usable
        rep = generate_report(session, "snapshot", cfg["language"],
                              use_llm=llm_on, created_by="monitor")
        report_id = rep.id
        sm = smtp_cfg(session)
        if sm.get("host") and sm.get("from"):
            if cfg["email_user"] and cfg["user_emails"]:
                send_report_email(session, rep, cfg["user_emails"], "user")
            if cfg["email_authorities"] and cfg["authorities_emails"] and actionable:
                send_report_email(session, rep, cfg["authorities_emails"], "authorities")
        cur["last_report_at"] = utcnow().isoformat()

    cur["last_run"] = utcnow().isoformat()
    cur.pop("burst_threshold", None)
    if state_row:
        # MERGE: prev holds scheduler-owned keys (last_enqueued) — replacing the
        # whole dict made the scheduler re-enqueue a tick every polling pass.
        state_row.value = {**prev, **cur}
    else:
        session.add(KV(key=STATE_KV, value=cur))
    session.commit()
    return {"findings": len(created_alerts), "report": report_id,
            "new_posts_since_last": cur.get("new_posts", 0)}


class MonitorScheduler(threading.Thread):
    """Thin trigger loop: owns NO analysis. It only enqueues durable jobs, so
    ticks survive restarts (the queue does the work)."""

    def __init__(self, worker: Worker | None = None):
        super().__init__(name="pickemup-monitor", daemon=True)
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick_gate()
            except Exception:  # noqa: BLE001 — scheduler must never die, but never hush
                traceback.print_exc()
            self._stop.wait(SCHED_INTERVAL_S)

    def _tick_gate(self) -> None:
        from .db import SessionLocal
        with SessionLocal() as s:
            cfg = get_monitor_config(s)
            if not cfg["enabled"]:
                return
            state = s.get(KV, STATE_KV)
            last = (state.value or {}).get("last_enqueued") if state else None
            from datetime import datetime, timedelta, timezone
            now = utcnow()
            if last:
                try:
                    if now - datetime.fromisoformat(last) < timedelta(minutes=cfg["interval_minutes"]):
                        return
                except ValueError:
                    pass
            slot = now.strftime("%Y-%m-%dT%H:%M")
            enqueue(s, "monitor_tick", {"slot": slot}, idempotency_key=f"monitor:{slot}")
            row = s.get(KV, STATE_KV)
            val = dict(row.value) if row and isinstance(row.value, dict) else {}
            val["last_enqueued"] = now.isoformat()
            if row:
                row.value = val
            else:
                s.add(KV(key=STATE_KV, value=val))
            s.commit()

    def stop(self) -> None:
        self._stop.set()
