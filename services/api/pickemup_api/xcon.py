"""X connection management from the UI: bearer token (write-only), spend caps,
live probe, and a confirm-gated data purge. Secrets policy mirrors llm.py:
values are stored, never returned — GET exposes a mask + booleans only."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .db import session_dep
from .models import AuditLog, KV, utcnow

router = APIRouter(prefix="/api/v1/x-connection", tags=["x-connection"])
PURGE_ROUTER = APIRouter(prefix="/api/v1/data", tags=["x-connection"])

KV_X = "x_connection"

# defaults mirror .env.example so a fresh install shows honest numbers
DEFAULT_PER_JOB_USD = 2.0
DEFAULT_PER_DAY_USD = 10.0


def _raw(session: Session) -> dict:
    row = session.get(KV, KV_X)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def effective_token(session: Session) -> str:
    """UI-saved token wins; process env (.env) is the fallback."""
    tok = _raw(session).get("token") or os.environ.get("X_BEARER_TOKEN", "")
    return tok.strip()


def caps(session: Session) -> tuple[float, float]:
    d = _raw(session)
    per_job = d.get("per_job_usd")
    per_day = d.get("per_day_usd")
    if per_job is None:
        try:
            per_job = float(os.environ.get("X_MAX_COST_PER_JOB_USD", DEFAULT_PER_JOB_USD))
        except ValueError:
            per_job = DEFAULT_PER_JOB_USD
    if per_day is None:
        try:
            per_day = float(os.environ.get("X_MAX_COST_PER_DAY_USD", DEFAULT_PER_DAY_USD))
        except ValueError:
            per_day = DEFAULT_PER_DAY_USD
    def _num(v, fallback):
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(fallback)
    return _num(per_job, DEFAULT_PER_JOB_USD), _num(per_day, DEFAULT_PER_DAY_USD)


def spend_today(session: Session) -> float:
    kv = session.get(KV, "budget")
    if not kv or not isinstance(kv.value, dict):
        return 0.0
    try:
        return float(kv.value.get(utcnow().strftime("%Y-%m-%d"), 0.0))
    except (TypeError, ValueError):
        return 0.0


def _mask(tok: str) -> str:
    if len(tok) <= 12:
        return "•" * len(tok)
    return f"{tok[:4]}… ({len(tok)} chars)"


@router.get("")
def get_connection(s: Session = Depends(session_dep)) -> dict:
    d = _raw(session=s)
    kv_token = bool(d.get("token"))
    env_token = bool(os.environ.get("X_BEARER_TOKEN", "").strip())
    tok = effective_token(s)
    per_job, per_day = caps(s)
    return {
        "token_set": bool(tok),
        "token_source": "settings" if kv_token else ("env" if env_token else "none"),
        "token_mask": _mask(tok) if tok else None,
        "per_job_usd": per_job, "per_day_usd": per_day,
        "caps_overridden": d.get("per_job_usd") is not None or d.get("per_day_usd") is not None,
        "spend_today_usd": spend_today(s),
        "updated_at": d.get("updated_at"),
    }


class XConIn(BaseModel):
    token: str | None = None          # None = keep; "" = clear; else store
    per_job_usd: float | None = Field(default=None, ge=0.0, le=10000.0)
    per_day_usd: float | None = Field(default=None, ge=0.0, le=100000.0)


@router.put("")
def put_connection(body: XConIn, s: Session = Depends(session_dep)) -> dict:
    d = _raw(s)
    if body.token is not None:
        t = body.token.strip()
        if t != body.token and not t:
            # whitespace-only is a paste mistake, not an intent to wipe the token
            raise HTTPException(422, "token is only whitespace — clear the field explicitly to remove it")
        if t:
            if len(t) < 20 or any(c.isspace() for c in t):
                raise HTTPException(422, "bearer token looks malformed (too short / contains whitespace)")
            d["token"] = t
        else:
            d.pop("token", None)
    if body.per_job_usd is not None:
        d["per_job_usd"] = float(body.per_job_usd)
    if body.per_day_usd is not None:
        d["per_day_usd"] = float(body.per_day_usd)
    d["updated_at"] = utcnow().isoformat()
    if (row := s.get(KV, KV_X)):
        row.value = d
    else:
        s.add(KV(key=KV_X, value=d))
    # value NEVER appears in the audit detail — only which fields changed
    s.add(AuditLog(actor="analyst", action="x_connection.update", subject="x_api",
                   detail={"token": ("cleared" if body.token == "" else
                                     "updated" if body.token is not None else "kept"),
                           "per_job_usd": body.per_job_usd, "per_day_usd": body.per_day_usd}))
    s.commit()
    return get_connection(s)


@router.post("/probe")
def probe(s: Session = Depends(session_dep)) -> dict:
    """ONE cheap authenticated read (/2/tweets/20) to verify token + tier.
    Free on X's side when denied; proves credentials without a search pull."""
    tok = effective_token(s)
    if not tok:
        raise HTTPException(409, "no bearer token configured")
    import httpx
    try:
        r = httpx.get("https://api.x.com/2/tweets/20",
                      headers={"Authorization": f"Bearer {tok}"}, timeout=20)
    except httpx.HTTPError as e:
        s.add(AuditLog(actor="analyst", action="x_connection.probe", subject="x_api",
                       detail={"ok": False, "error": type(e).__name__}))
        s.commit()
        return {"ok": False, "status": 0, "message": f"network error: {type(e).__name__}"}
    meaning = {
        200: "token valid — this tier can read (probe tweet fetched)",
        401: "token invalid or revoked — regenerate at developer.x.com",
        402: "token valid, but the project has NO CREDITS — top up at developer.x.com → Billing",
        403: "token valid, but this endpoint/tier is not permitted",
        429: "rate limited — X reachable; try again later",
    }
    msg = meaning.get(r.status_code, f"HTTP {r.status_code}")
    s.add(AuditLog(actor="analyst", action="x_connection.probe", subject="x_api",
                   detail={"ok": r.status_code == 200, "status": r.status_code}))
    s.commit()
    return {"ok": r.status_code == 200, "status": r.status_code, "message": msg}


class PurgeIn(BaseModel):
    confirm: str  # must be exactly "PURGE"


@PURGE_ROUTER.post("/purge")
def purge(body: PurgeIn, s: Session = Depends(session_dep)) -> dict:
    """Hard-delete every collected/derived record. Configuration (X connection,
    LLM, SMTP, monitor settings, watchlist terms) and the audit trail SURVIVE:
    audit is append-only by policy; watchlist vocabulary is operator knowledge,
    not test residue."""
    if body.confirm != "PURGE":
        raise HTTPException(422, 'pass {"confirm": "PURGE"} to wipe collected + derived data')
    from .models import (Account, Alert, Case, CaseMember, CaseNote, DetectionResult,
                         Entity, EntityPost, Job, Post, Report, ReportDelivery)
    counts = {}
    for label, stmt in [
        ("entity_post", delete(EntityPost)), ("entity", delete(Entity)),
        ("detections", delete(DetectionResult)), ("alerts", delete(Alert)),
        ("report_deliveries", delete(ReportDelivery)), ("reports", delete(Report)),
        ("case_notes", delete(CaseNote)), ("case_members", delete(CaseMember)),
        ("cases", delete(Case)), ("posts", delete(Post)), ("accounts", delete(Account)),
        ("jobs", delete(Job)),
    ]:
        res = s.execute(stmt)
        counts[label] = int(getattr(res, "rowcount", 0) or 0)
    # reset spend counters + monitor baseline so caps/first-tick logic start clean;
    # keep the settings KVs (x_connection, llm_config, monitor_config, smtp...)
    for key in ("budget", "monitor_state"):
        row = s.get(KV, key)
        if row:
            s.delete(row)
    s.add(AuditLog(actor="analyst", action="data.purge", subject="corpus", detail=counts))
    s.commit()
    return {"purged": counts, "kept": ["x_connection", "llm/smtp/monitor settings",
                                       "watchlist terms", "audit log"]}
