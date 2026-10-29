"""Data-driven correlation rules — `correlations/*.yaml` specs
evaluated over the stored
event/entity index, each hit linked to its evidence.

Rules run on every monitor tick (and on demand). Every alert they raise is
deduplicated per rule+subject+day and carries evidence ids so a human can
re-check the underlying records.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import session_dep
from .models import Alert, AuditLog, Entity, EntityPost, Post, utcnow
from .pipeline import _raise_alert
from .risk_lexicon import load_custom_terms

router = APIRouter(prefix="/api/v1/correlations", tags=["correlations"])


# ---------------- rules ----------------

def _rule_watchlist_link(s: Session) -> list[dict]:
    """Watch vocabulary + external link = distribution attempt worth review."""
    terms = load_custom_terms(s)
    if not terms:
        return []
    domain_of: dict[str, str] = {}
    for value, post_id in s.execute(
            select(Entity.value, EntityPost.post_id)
            .join(EntityPost, EntityPost.entity_id == Entity.id)
            .where(Entity.etype == "DOMAIN")).all():  # columns only, no ORM fan-out
        domain_of.setdefault(post_id, value)
    hits = []
    for t in terms:
        tl = t.term.lower()
        pat = "%" + tl.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        posts = s.scalars(select(Post).where(Post.deleted.is_(False),
                                             Post.text.ilike(pat, escape="\\"))).all()
        for p in posts:
            if p.id in domain_of:
                hits.append({"subject": p.id, "risk": "MEDIUM",
                             "headline": f"watch term '{t.term}' amplifying link {domain_of[p.id]}",
                             "evidence": {"post_id": p.id, "term": t.term,
                                          "domain": domain_of[p.id], "author": p.author_id}})
                break  # one hit per term keeps alerts signal-dense
    return hits


def _rule_domain_spread(s: Session) -> list[dict]:
    """A link shared across several accounts where cluster members participate
    = possible coordinated distribution of one asset (local data only)."""
    from .models import DetectionResult
    clustered: set[str] = set()
    for r in s.scalars(select(DetectionResult).where(DetectionResult.kind == "cluster")).all():
        clustered.update((r.payload or {}).get("members") or [])
    if not clustered:
        return []
    doms = s.scalars(select(Entity).where(Entity.etype == "DOMAIN",
                                          Entity.post_count >= 4)).all()
    # ONE query for every candidate domain's links (was N+1 per hot domain)
    by_eid: dict[int, set[str]] = {}
    posts_by_eid: dict[int, list[str]] = {}
    for eid, author, post in s.execute(select(EntityPost.entity_id, EntityPost.author_id,
                                              EntityPost.post_id)
                                       .where(EntityPost.entity_id.in_([e.id for e in doms]))).all():
        by_eid.setdefault(eid, set()).add(author)
        posts_by_eid.setdefault(eid, []).append(post)
    out = []
    for e in doms:
        authors = by_eid.get(e.id, set())
        in_cluster = authors & clustered
        if len(authors) >= 4 and len(in_cluster) >= 2:
            out.append({"subject": e.value, "risk": "MEDIUM",
                        "headline": f"link {e.value} spread by {len(authors)} accounts "
                                    f"({len(in_cluster)} in detected clusters)",
                        "evidence": {"domain": e.value, "authors": sorted(authors)[:10],
                                     "cluster_accounts": sorted(in_cluster)[:6],
                                     "post_ids": sorted(set(posts_by_eid.get(e.id, [])))[:8]}})
    return out


RULES = [
    {"id": "watchlist_plus_link", "name": "Watch vocabulary amplifying an external link",
     "risk": "MEDIUM", "fn": _rule_watchlist_link,
     "description": "A post matching an operator watchlist term also carries a link — "
                    "possible distribution attempt worth review."},
    {"id": "domain_across_clusters", "name": "Same link across cluster accounts",
     "risk": "MEDIUM", "fn": _rule_domain_spread,
     "description": "One domain shared by 4+ accounts where 2+ belong to detected "
                    "coordination clusters."},
]


def run_correlations(s: Session) -> dict:
    """Evaluate all rules; raise deduplicated alerts with evidence links."""
    day = utcnow().strftime("%Y%m%d")
    raised: list[dict] = []
    for rule in RULES:
        try:
            hits = rule["fn"](s)
        except Exception as e:  # noqa: BLE001 — a rule must never break the tick
            # a DBAPI error leaves the session unusable: roll back FIRST, then
            # audit in its own transaction (an audit failure must not raise)
            try:
                s.rollback()
                s.add(AuditLog(actor="worker", action="correlation.error", subject=rule["id"],
                               detail={"error": str(e)[:200]}))
                s.commit()
            except Exception:  # noqa: BLE001
                s.rollback()
            continue
        for h in hits:
            dedupe = f"corr:{rule['id']}:{h['subject']}:{day}"
            if s.scalar(select(Alert).where(Alert.dedupe_key == dedupe)):
                continue
            try:
                _raise_alert(s, kind=f"correlation.{rule['id']}",
                             severity="medium" if h["risk"] == "MEDIUM" else "low",
                             dedupe=dedupe, subject=h["subject"],
                             evidence={"headline": h["headline"], **h["evidence"]})
                s.commit()  # per-hit commit: a dedupe race loses alone, not the batch
                raised.append({"rule": rule["id"], **h})
            except IntegrityError:  # concurrent run (tick vs POST /run) inserted first
                s.rollback()
            except Exception as e:  # noqa: BLE001
                s.rollback()
                try:
                    s.add(AuditLog(actor="worker", action="correlation.error",
                                   subject=f"{rule['id']}:hit", detail={"error": str(e)[:200]}))
                    s.commit()
                except Exception:  # noqa: BLE001
                    s.rollback()
    if raised:
        try:
            s.add(AuditLog(actor="worker", action="correlations.run", subject="ruleset",
                           detail={"raised": len(raised), "rules": len(RULES)}))
            s.commit()
        except Exception:  # noqa: BLE001
            s.rollback()
    return {"rules_evaluated": len(RULES), "raised": len(raised), "hits": raised}


# ---------------- routes ----------------

@router.get("")
def list_rules(s: Session = Depends(session_dep)) -> dict:
    day = utcnow().strftime("%Y%m%d")
    recent = s.scalars(select(Alert).where(Alert.kind.like("correlation.%"),
                                           Alert.dedupe_key.like(f"%{day}"))
                       .order_by(Alert.created_at.desc()).limit(50)).all()
    return {"rules": [{k: r[k] for k in ("id", "name", "risk", "description")} for r in RULES],
            "today_hits": [{"alert_id": a.id, "kind": a.kind, "subject": a.subject_id,
                            "evidence": a.evidence} for a in recent]}


@router.post("/run")
def run_now(s: Session = Depends(session_dep)) -> dict:
    if not s.scalar(select(Post).limit(1)):
        raise HTTPException(409, "no data in corpus")
    return run_correlations(s)
