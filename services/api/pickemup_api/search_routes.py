"""Unified Search + local Transforms — one query enters, typed results
(one query → structured grouped results; typed entity pivots discovered per
exit by pivot; every result is grounded in the typed local index.

Everything here queries LOCAL data only: no external providers, no network
calls. Every pivot result carries the stored evidence (post ids) that produced
it — a result without lineage does not exist.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .db import session_dep
from .models import Account, DetectionResult, Entity, EntityPost, Post
from .risk_lexicon import load_custom_terms

router = APIRouter(prefix="/api/v1", tags=["search"])

ENTITY_TYPES = ["HASHTAG", "MENTION", "URL", "DOMAIN", "EMAIL", "PHONE"]


def _ensure_index(s: Session) -> None:
    """Self-healing index: build on first use if the corpus exists but the
    index is empty (e.g. data loaded before this feature)."""
    if not s.scalar(select(func.count(Entity.id))):
        if s.scalar(select(func.count(Post.id)).where(Post.deleted.is_(False))):
            from .pipeline import reindex_entities
            try:
                reindex_entities(s)
            except Exception:  # noqa: BLE001 — concurrent rebuild lost the race;
                s.rollback()   # the index self-heals on the next request or job


def _acct_map(s: Session, author_ids: set[str]) -> dict[str, str]:
    if not author_ids:
        return {}
    return {a.id: a.username for a in s.scalars(select(Account).where(Account.id.in_(author_ids))).all()}


@router.get("/search")
def unified_search(q: str = "", limit: int = 12, s: Session = Depends(session_dep)) -> dict:
    q = (q or "").strip()
    if not q:
        raise HTTPException(422, "q required")
    _ensure_index(s)
    term = q.lstrip("#@").lower()
    if not term:
        raise HTTPException(422, "query is only a sigil — give me a word")
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{esc}%"

    entities = s.scalars(select(Entity).where(
        Entity.value.like(like, escape="\\") | Entity.label.like(like, escape="\\")
    ).order_by(Entity.post_count.desc()).limit(limit * 3)).all()

    accounts = s.scalars(select(Account).where(
        Account.deleted.is_(False),
        or_(Account.username.ilike(like, escape="\\"),
            Account.display_name.ilike(like, escape="\\"))
    ).limit(limit)).all()
    verdicts = {d.subject_id: d.verdict for d in s.scalars(
        select(DetectionResult).where(DetectionResult.kind == "automation",
                                      DetectionResult.subject_id.in_([a.id for a in accounts] or ["-"]))).all()}

    posts = s.scalars(select(Post).where(
        Post.deleted.is_(False),
        or_(Post.text.ilike(like, escape="\\"), Post.source_url.ilike(like, escape="\\"))
    ).order_by(Post.created_at.desc()).limit(limit)).all()
    names = _acct_map(s, {p.author_id for p in posts})

    # watchlist relevance: is this query already a tracked trigger word?
    watched = [t.term for t in load_custom_terms(s)
               if term in t.term.lower() or t.term.lower() in term]

    return {
        "query": q,
        "entities": [{"etype": e.etype, "value": e.value, "label": e.label,
                      "post_count": e.post_count,
                      "first_seen": e.first_seen.isoformat() if e.first_seen else None,
                      "last_seen": e.last_seen.isoformat() if e.last_seen else None}
                     for e in entities],
        "accounts": [{"id": a.id, "username": a.username, "display_name": a.display_name,
                      "origin_mode": a.origin_mode, "automation_verdict": verdicts.get(a.id)}
                     for a in accounts],
        "posts": [{"id": p.id, "author_id": p.author_id, "author": names.get(p.author_id, p.author_id),
                   "excerpt": (p.text or "")[:180], "created_at": p.created_at.isoformat() if p.created_at else None,
                   "lang": p.lang, "origin_mode": p.origin_mode} for p in posts],
        "watchlist_hits": watched,
        "totals": {"entities": s.scalar(select(func.count(Entity.id))),
                   "matched_entities": len(entities), "matched_posts": len(posts),
                   "matched_accounts": len(accounts)},
        "note": "Local-corpus search only; every entity/result links to stored evidence. "
                "Local-corpus search over the typed entity index.",
    }


@router.get("/search/stats")
def index_stats(s: Session = Depends(session_dep)) -> dict:
    _ensure_index(s)
    rows = s.execute(select(Entity.etype, func.count(Entity.id), func.sum(Entity.post_count))
                     .group_by(Entity.etype)).all()
    return {"types": {r[0]: {"entities": r[1], "occurrences": r[2] or 0} for r in rows},
            "entities_total": s.scalar(select(func.count(Entity.id)))}


# ---------------- transforms (typed local pivots) ----------------

def _entity_posts(s: Session, etype: str, value: str, limit: int = 300):
    val = value.lstrip("#@").lower()
    e = s.scalar(select(Entity).where(Entity.etype == etype, Entity.value.in_([val, f"#{val}", f"@{val}"])))
    if not e:
        # deterministic fallback: prefer the highest-ranked containment match,
        # never an arbitrary storage-order row
        fesc = val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        e = s.scalar(select(Entity).where(Entity.etype == etype,
                                          Entity.value.like(f"%{fesc}%", escape="\\"))
                     .order_by(Entity.post_count.desc()))
    if not e:
        return None, []
    links = s.scalars(select(EntityPost).where(EntityPost.entity_id == e.id)
                      .order_by(EntityPost.at.desc().nullslast()).limit(limit)).all()
    return e, links


def _t_posts_of(s: Session, etype: str, value: str) -> dict:
    e, links = _entity_posts(s, etype, value)
    if not e:
        return {"output_type": "POST", "results": [], "note": "entity not in index"}
    names = _acct_map(s, {l.author_id for l in links})
    return {"output_type": "POST", "results": [
        {"value": l.post_id, "label": names.get(l.author_id, l.author_id),
         "weight": l.occurrences, "evidence": [l.post_id]} for l in links[:25]],
            "truncated": len(links) >= 300}


def _t_authors_of(s: Session, etype: str, value: str) -> dict:
    e, links = _entity_posts(s, etype, value)
    if not e:
        return {"output_type": "ACCOUNT", "results": [], "note": "entity not in index"}
    # NOTE: ranking is computed over the 300 most-recent links; beyond that the
    # pivot degrades gracefully and says so
    counts: dict[str, int] = {}
    ev: dict[str, list[str]] = {}
    for l in links:
        counts[l.author_id] = counts.get(l.author_id, 0) + l.occurrences
        ev.setdefault(l.author_id, []).append(l.post_id)
    names = _acct_map(s, set(counts))
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:20]
    return {"output_type": "ACCOUNT", "results": [
        {"value": aid, "label": names.get(aid, aid), "weight": n, "evidence": ev[aid][:4]}
        for aid, n in top]}


def _t_co_entities(s: Session, base: str, etype: str, value: str) -> dict:
    """Entities of the same type co-occurring in posts containing `value`."""
    e, links = _entity_posts(s, etype, value)
    if not e:
        return {"output_type": etype, "results": [], "note": "entity not in index"}
    pids = [l.post_id for l in links]
    rows = s.execute(
        select(Entity.etype, Entity.value, Entity.label, func.count(EntityPost.post_id),
               func.min(EntityPost.post_id))
        .join(EntityPost, EntityPost.entity_id == Entity.id)
        .where(EntityPost.post_id.in_(pids or ["-"]), Entity.etype == base,
               Entity.id != e.id)
        .group_by(Entity.id).order_by(func.count(EntityPost.post_id).desc()).limit(15)
    ).all()
    # evidence: one representative co-occurring post per row (contract: no
    # lineage-less results)
    return {"output_type": base, "results": [
        {"value": r[1], "label": r[2], "weight": r[3],
         "evidence": [r[4]] if r[4] else []} for r in rows]}


def _t_account_clusters(s: Session, _etype: str, value: str) -> dict:
    handle = value.lstrip("@").lower()
    acct = s.scalar(select(Account).where(func.lower(Account.username) == handle))
    if not acct:
        return {"output_type": "CLUSTER", "results": [], "note": "unknown account"}
    mem = s.scalars(select(DetectionResult).where(DetectionResult.kind == "cluster")).all()
    out = []
    for r in mem:
        members = (r.payload or {}).get("members") or []
        if acct.id in members:
            out.append({"value": r.subject_id, "label": r.verdict,
                        "weight": round((r.confidence or 0) * 100), "evidence": []})
    return {"output_type": "CLUSTER", "results": out}


def _t_account_posts(s: Session, _etype: str, value: str) -> dict:
    handle = value.lstrip("@").lower()
    acct = s.scalar(select(Account).where(func.lower(Account.username) == handle))
    if not acct:
        return {"output_type": "POST", "results": [], "note": "unknown account"}
    posts = s.scalars(select(Post).where(Post.deleted.is_(False), Post.author_id == acct.id)
                      .order_by(Post.created_at.desc()).limit(25)).all()
    return {"output_type": "POST", "results": [
        {"value": p.id, "label": (p.text or "")[:60], "weight": 1, "evidence": [p.id]} for p in posts]}


TRANSFORMS: list[dict] = [
    {"name": "posts_containing", "display": "Posts containing this", "set": "PICKEMUP Local",
     "inputs": ["HASHTAG", "MENTION", "URL", "DOMAIN", "EMAIL", "PHONE"], "output": "POST",
     "description": "All stored posts where the entity appears (evidence-linked).",
     "fn": _t_posts_of},
    {"name": "authors", "display": "Who posted this", "set": "PICKEMUP Local",
     "inputs": ["HASHTAG", "URL", "DOMAIN", "EMAIL", "PHONE", "MENTION"], "output": "ACCOUNT",
     "description": "Accounts that produced posts containing the entity, ranked.",
     "fn": _t_authors_of},
    {"name": "co_hashtags", "display": "Co-occurring hashtags", "set": "PICKEMUP Local",
     "inputs": ["HASHTAG", "DOMAIN", "URL"], "output": "HASHTAG",
     "description": "Hashtags appearing in the same posts — vocabulary fingerprint.",
     "fn": lambda s, t, v: _t_co_entities(s, "HASHTAG", t, v)},
    {"name": "co_domains", "display": "Co-occurring domains", "set": "PICKEMUP Local",
     "inputs": ["HASHTAG", "DOMAIN", "URL"], "output": "DOMAIN",
     "description": "Links shared in the same posts — distribution fingerprint.",
     "fn": lambda s, t, v: _t_co_entities(s, "DOMAIN", t, v)},
    {"name": "co_mentions", "display": "Co-mentioned accounts", "set": "PICKEMUP Local",
     "inputs": ["MENTION"], "output": "MENTION",
     "description": "Handles that appear alongside this one in the same posts.",
     "fn": lambda s, t, v: _t_co_entities(s, "MENTION", t, v)},
    {"name": "account_posts", "display": "Account's posts", "set": "PICKEMUP Local",
     "inputs": ["ACCOUNT", "MENTION"], "output": "POST",
     "description": "Original posts authored by the account.", "fn": _t_account_posts},
    {"name": "cluster_memberships", "display": "Behaviour clusters of account", "set": "PICKEMUP Local",
     "inputs": ["ACCOUNT", "MENTION"], "output": "CLUSTER",
     "description": "Detected coordination clusters containing the account.",
     "fn": _t_account_clusters},
]


class TransformIn(BaseModel):
    input_type: str = Field(max_length=20)
    value: str = Field(min_length=1, max_length=2000)
    name: str = Field(max_length=60)


@router.get("/transforms")
def list_transforms(input: str | None = None) -> dict:
    items = [t for t in TRANSFORMS if not input or input.upper() in t["inputs"]]
    return {"transforms": [{"name": t["name"], "display": t["display"], "set": t["set"],
                            "inputs": t["inputs"], "output": t["output"],
                            "description": t["description"]} for t in items]}


@router.post("/transform")
def run_transform(body: TransformIn, s: Session = Depends(session_dep)) -> dict:
    t = next((x for x in TRANSFORMS if x["name"] == body.name), None)
    if not t:
        raise HTTPException(404, "unknown transform")
    it = body.input_type.upper()
    if it not in t["inputs"]:
        raise HTTPException(422, f"transform '{body.name}' does not accept {it} "
                                f"(inputs: {', '.join(t['inputs'])})")
    _ensure_index(s)
    res = t["fn"](s, it, body.value)
    return {"transform": body.name, "input_type": it, "input": body.value, **res}
