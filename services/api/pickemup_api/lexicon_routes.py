"""Trigger-word watchlist API.

Operators extend the detection vocabulary at runtime (new terrorist/offender
slang, campaign hashtags, group aliases). Every mutation re-enqueues the
content-risk job, so the whole corpus is re-scored against the updated lexicon
within seconds — the database stays up to date without manual intervention.
All changes are audited.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import session_dep
from .jobs import enqueue
from .models import AuditLog, CustomTerm
from .risk_lexicon import (SEVERITY_WEIGHT, TERM_RE, corpus_hits, corpus_hits_many,
                           load_custom_terms, matrix_terms, search_matrix)

router = APIRouter(prefix="/api/v1/lexicon", tags=["lexicon"])


class TermIn(BaseModel):
    term: str = Field(min_length=1, max_length=78)
    severity: str = "watch"
    note: str = Field(default="", max_length=200)


class TermPatch(BaseModel):
    severity: str | None = None
    note: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None


def _clean(term: str) -> str:
    t = " ".join((term or "").split())  # collapse whitespace incl. newlines/tabs
    if not t or len(t) > 78 or not TERM_RE.match(t):
        raise HTTPException(422, "term must be 1-78 letters/digits/space/punctuation, no control characters")
    return t


def _rescore(s: Session) -> int:
    """Queue a corpus-wide re-analysis keyed to the current lexicon state."""
    j = enqueue(s, "analyze_content_risk", {"reason": "lexicon_update", "ts": time.time()})
    return j.id


@router.get("")
def list_terms(s: Session = Depends(session_dep)) -> dict:
    rows = list(s.scalars(select(CustomTerm).order_by(CustomTerm.created_at.desc())).all())
    cats = matrix_terms()
    hits_map = corpus_hits_many(s, [r.term for r in rows])  # ONE scan for all rows
    return {
        "custom": [{"id": r.id, "term": r.term, "severity": r.severity, "note": r.note,
                    "enabled": r.enabled, "weight": SEVERITY_WEIGHT.get(r.severity, 0.45),
                    "hits": hits_map.get(r.term.lower(), 0), "created_by": r.created_by,
                    "created_at": r.created_at.isoformat()} for r in rows],
        "built_in": {"categories": cats, "total_terms": sum(cats.values())},
        "severity_weights": SEVERITY_WEIGHT,
        "active_terms": len(load_custom_terms(s)),
    }


@router.get("/search")
def find_term(q: str = "", s: Session = Depends(session_dep)) -> dict:
    """'Is this word already covered?' — searches the built-in matrix and the
    operator watchlist together."""
    qn = (q or "").strip().lower()
    if not qn:
        raise HTTPException(422, "q required")
    own = list(s.scalars(select(CustomTerm).where(
        func.lower(CustomTerm.term).like(f"%{qn}%"))).all())
    hits_map = corpus_hits_many(s, [x.term for x in own])
    return {"query": qn,
            "built_in": search_matrix(qn),
            "watchlist": [{"id": r.id, "term": r.term, "severity": r.severity,
                           "enabled": r.enabled,
                           "hits": hits_map.get(r.term.lower(), 0)} for r in own]}


@router.post("")
def add_term(body: TermIn, s: Session = Depends(session_dep)) -> dict:
    t = _clean(body.term)
    sev = body.severity.strip().lower()
    if sev not in SEVERITY_WEIGHT:
        raise HTTPException(422, f"severity must be one of {sorted(SEVERITY_WEIGHT)}")
    dup = s.scalar(select(CustomTerm.id).where(func.lower(CustomTerm.term) == t.lower()))
    if dup:
        raise HTTPException(409, f"'{t}' is already on the watchlist")
    already = [m for m in search_matrix(t, limit=50) if m["term"].lower() == t.lower()]
    row = CustomTerm(term=t, severity=sev, note=body.note.strip())
    s.add(row)
    s.add(AuditLog(actor="analyst", action="lexicon.add", subject=t,
                   detail={"severity": sev, "note": row.note,
                           "already_in_matrix": bool(already)}))
    try:
        s.commit()
    except IntegrityError:  # concurrent POST of Foo/foo won the unique race
        s.rollback()
        raise HTTPException(409, f"'{t}' is already on the watchlist")
    job_id = _rescore(s)
    return {"id": row.id, "term": t, "severity": sev, "already_in_matrix": bool(already),
            "rescore_job": job_id,
            "note": "corpus re-scoring queued — hits appear on the Threat Monitor shortly"}


@router.patch("/{term_id}")
def patch_term(term_id: int, body: TermPatch, s: Session = Depends(session_dep)) -> dict:
    row = s.get(CustomTerm, term_id)
    if not row:
        raise HTTPException(404, "unknown term")
    patch = body.model_dump(exclude_none=True)
    if "severity" in patch:
        patch["severity"] = patch["severity"].strip().lower()
        if patch["severity"] not in SEVERITY_WEIGHT:
            raise HTTPException(422, "bad severity")
    if not patch:
        raise HTTPException(422, "nothing to change")
    for k, v in patch.items():
        setattr(row, k, v)
    s.add(AuditLog(actor="analyst", action="lexicon.update", subject=row.term, detail=patch))
    s.commit()
    job_id = _rescore(s)
    return {"id": row.id, "term": row.term, "severity": row.severity,
            "enabled": row.enabled, "rescore_job": job_id}


@router.delete("/{term_id}")
def delete_term(term_id: int, s: Session = Depends(session_dep)) -> dict:
    row = s.get(CustomTerm, term_id)
    if not row:
        raise HTTPException(404, "unknown term")
    term = row.term
    s.delete(row)
    s.add(AuditLog(actor="analyst", action="lexicon.delete", subject=term,
                   detail={"severity": row.severity}))
    s.commit()
    job_id = _rescore(s)
    return {"deleted": term, "rescore_job": job_id}
