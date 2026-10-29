"""Optional-LLM endpoints: connection settings, reports, investigation, delivery, monitoring.

Every route here degrades honestly: with no LLM configured, `status` reports
`usable: false`, report generation returns the deterministic template, and the
investigator returns 400 explaining what to enable. Nothing 500s on a missing
model. Secrets are never echoed back.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import session_dep
import logging

from .llm import (KV_KEY, LLMError, get_config, probe as llm_probe, public_status,
                  save_config, validate_base_url)

logger = logging.getLogger("pickemup.ai")
from .models import AuditLog, KV, Report, ReportDelivery, utcnow
from .monitor import STATE_KV, get_monitor_config, save_monitor_config
from .reporting import (SMTP_KV, generate_report, investigator_tools,
                        send_report_email, validate_recipients)

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


class LLMConfigIn(BaseModel):
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str | None = None          # omitted/None keeps the stored key
    temperature: float = Field(0.2, ge=0.0, le=1.5)
    max_tokens: int = Field(1600, ge=128, le=8192)
    timeout_s: float = Field(120.0, ge=5.0, le=600.0)


@router.get("/config")
def read_llm_config(s: Session = Depends(session_dep)) -> dict:
    return public_status(s)


@router.get("/status")
def llm_status(s: Session = Depends(session_dep)) -> dict:
    cfg = get_config(s)
    return {"configured": bool(cfg.base_url and cfg.model), "usable": cfg.usable,
            "enabled": cfg.enabled, "model": cfg.model, "source": cfg.source,
            "tools": [t.name for t in investigator_tools(s)] if cfg.usable else [],
            "fallback": "deterministic template reports remain available without an LLM"}


@router.put("/config")
def write_llm_config(body: LLMConfigIn, s: Session = Depends(session_dep)) -> dict:
    if body.base_url.strip():
        err = validate_base_url(body.base_url)
        if err:
            raise HTTPException(422, err)
    cfg = get_config(s)
    # key retention reads the STORED KV row (not get_config's env fallback):
    # an env-provided key must never be copied into the database
    row = s.get(KV, KV_KEY)
    stored_key = (row.value or {}).get("api_key") if row and isinstance(row.value, dict) else None
    cfg.enabled = body.enabled
    cfg.base_url = body.base_url.strip().rstrip("/")
    cfg.model = body.model.strip()
    cfg.api_key = stored_key if body.api_key is None else body.api_key
    cfg.temperature, cfg.max_tokens, cfg.timeout_s = body.temperature, body.max_tokens, body.timeout_s
    cfg.source = "settings"
    if cfg.enabled and not cfg.usable:
        raise HTTPException(422, "enabled requires base_url and model")
    try:
        save_config(s, cfg)  # save_config re-validates base_url
    except ValueError as e:
        raise HTTPException(422, str(e))
    s.add(AuditLog(actor="analyst", action="ai.config.update", subject="llm",
                   detail={"enabled": cfg.enabled, "base_url": cfg.base_url,
                           "model": cfg.model, "key_set": bool(cfg.api_key)}))
    s.commit()
    return public_status(s)


@router.delete("/config")
def clear_llm_config(s: Session = Depends(session_dep)) -> dict:
    row = s.get(KV, "llm_config")
    if row:
        s.delete(row)
    s.add(AuditLog(actor="analyst", action="ai.config.clear", subject="llm", detail={}))
    s.commit()
    return {"ok": True}


@router.post("/probe")
def probe_llm(s: Session = Depends(session_dep)) -> dict:
    """Round-trip test against the configured provider (Settings -> Test connection)."""
    return llm_probe(s)


class ReportIn(BaseModel):
    scope: str = Field("snapshot", max_length=120)
    language: str = Field("en", pattern="^(en|ar)$")
    use_llm: bool = True


@router.post("/reports", status_code=201)
def create_report(body: ReportIn, s: Session = Depends(session_dep)) -> dict:
    try:
        rep = generate_report(s, body.scope, body.language, body.use_llm)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001 — never leak internals to the client
        logger.exception("report generation failed")
        raise HTTPException(500, f"report generation failed ({type(e).__name__})")
    return _report_json(rep, include_facts=False)


@router.get("/reports")
def list_reports(limit: int = 30, scope: str | None = None, s: Session = Depends(session_dep)) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    qs = select(Report).order_by(Report.id.desc()).limit(limit)
    if scope:
        qs = select(Report).where(Report.scope == scope).order_by(Report.id.desc()).limit(limit)
    return [_report_json(r, False) for r in s.scalars(qs).all()]


@router.get("/reports/{report_id}")
def get_report(report_id: str, s: Session = Depends(session_dep)) -> dict:
    rep = s.get(Report, report_id)
    if not rep:
        raise HTTPException(404, "report not found")
    sends = s.scalars(select(ReportDelivery).where(ReportDelivery.report_id == report_id)
                      .order_by(ReportDelivery.id)).all()
    return _report_json(rep, include_facts=True,
                        extra={"deliveries": [{"id": d.id, "channel": d.channel,
                                               "recipients": d.recipients, "kind": d.recipient_kind,
                                               "status": d.status, "detail": d.detail,
                                               "at": d.created_at.isoformat()} for d in sends]})


def _report_json(rep: Report, include_facts: bool, extra: dict | None = None) -> dict:
    d = {"id": rep.id, "scope": rep.scope, "title": rep.title, "language": rep.language,
         "mode": rep.mode, "model": rep.model, "content_md": rep.content_md,
         "facts_sha256": rep.facts_sha256, "llm_error": rep.llm_error,
         "created_by": rep.created_by, "created_at": rep.created_at.isoformat()}
    if include_facts:
        d["facts"] = rep.facts
    return {**d, **(extra or {})}


class InvestigateIn(BaseModel):
    focus: str = Field("", max_length=300)
    max_steps: int = Field(6, ge=1, le=12)


@router.post("/investigate", status_code=201)
def investigate(body: InvestigateIn, s: Session = Depends(session_dep)) -> dict:
    from .reporting import investigate as _inv
    try:
        rep = _inv(s, body.focus, body.max_steps)
    except LLMError as e:
        raise HTTPException(409, str(e))
    except Exception:  # noqa: BLE001 — provider bodies/stacks must not reach clients
        logger.exception("investigation failed")
        raise HTTPException(502, "investigation failed (see server log)")
    return _report_json(rep, include_facts=True)


class SendIn(BaseModel):
    recipients: list[str]
    kind: str = Field("user", pattern="^(user|authorities|custom)$")


@router.post("/reports/{report_id}/send")
def send_report(report_id: str, body: SendIn, s: Session = Depends(session_dep)) -> dict:
    rep = s.get(Report, report_id)
    if not rep:
        raise HTTPException(404, "report not found")
    try:
        to = validate_recipients(body.recipients)
    except ValueError as e:
        raise HTTPException(422, str(e))
    try:
        d = send_report_email(s, rep, to, body.kind)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except Exception:  # noqa: BLE001 — SMTP errors may embed credentials in URLs
        logger.exception("report delivery failed")
        raise HTTPException(502, "send failed (see server log)")
    return {"id": d.id, "status": d.status, "recipients": d.recipients,
            "kind": d.recipient_kind, "detail": d.detail}


# ---- SMTP + monitor settings (same /ai surface so the UI has one home) ----

class SMTPIn(BaseModel):
    host: str = ""
    port: int = 587
    username: str = ""
    password: str | None = None
    from_address: str = ""
    tls: bool = True


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


@router.get("/smtp")
def read_smtp(s: Session = Depends(session_dep)) -> dict:
    row = s.get(KV, SMTP_KV)
    v = dict(row.value) if row and isinstance(row.value, dict) else {}
    return {"host": v.get("host", ""), "port": v.get("port", 587),
            "username": v.get("username", ""), "from": v.get("from", ""),
            "tls": v.get("tls", True), "has_password": bool(v.get("password")),
            "configured": bool(v.get("host") and v.get("from"))}


@router.put("/smtp")
def write_smtp(body: SMTPIn, s: Session = Depends(session_dep)) -> dict:
    row = s.get(KV, SMTP_KV)
    v = dict(row.value) if row and isinstance(row.value, dict) else {}
    if body.host and not re.match(r"^[A-Za-z0-9._-]+$", body.host):
        raise HTTPException(422, "smtp host must be a plain hostname")
    if body.from_address and not EMAIL_RE.match(body.from_address):
        raise HTTPException(422, "invalid from address")
    v.update({"host": body.host.strip(), "port": body.port, "username": body.username.strip(),
              "from": body.from_address.strip(), "tls": body.tls})
    if body.password is not None:
        v["password"] = body.password
    if row:
        row.value = v
    else:
        s.add(KV(key=SMTP_KV, value=v))
    s.add(AuditLog(actor="analyst", action="ai.smtp.update", subject="smtp",
                   detail={"host": v["host"], "from": v["from"]}))
    s.commit()
    return read_smtp(s)


class MonitorIn(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None
    auto_report: bool | None = None
    email_user: bool | None = None
    email_authorities: bool | None = None
    user_emails: list[str] | None = None
    authorities_emails: list[str] | None = None
    language: str | None = None
    burst_post_threshold: int | None = None


@router.get("/monitor")
def read_monitor(s: Session = Depends(session_dep)) -> dict:
    cfg = get_monitor_config(s)
    row = s.get(KV, STATE_KV)
    last = (row.value or {}).get("last_run") if row else None
    return {**cfg, "last_run": last}


@router.put("/monitor")
def write_monitor(body: MonitorIn, s: Session = Depends(session_dep)) -> dict:
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    for key in ("user_emails", "authorities_emails"):
        for e in patch.get(key, []):
            if not EMAIL_RE.match(str(e).strip().lower()):
                raise HTTPException(422, f"invalid email in {key}: {e!r}")
    if "language" in patch and patch["language"] not in ("en", "ar"):
        raise HTTPException(422, "language must be en or ar")
    cfg = save_monitor_config(s, patch)
    s.add(AuditLog(actor="analyst", action="ai.monitor.update", subject="monitor",
                   detail={k: v for k, v in patch.items() if not k.endswith("emails")}))
    s.commit()
    row = s.get(KV, STATE_KV)
    return {**cfg, "last_run": (row.value or {}).get("last_run") if row else None}
