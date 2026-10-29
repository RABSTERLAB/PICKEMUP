"""Job handlers: ingestion (connectors -> DB w/ provenance+evidence) and
analysis (engines -> DetectionResult rows). Idempotent upserts throughout.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pickemup_analysis import ALGO_VERSION
from pickemup_analysis.automation import score_account
from pickemup_analysis.content_bot import classify_posts
from pickemup_analysis.risk import classify as risk_classify
from pickemup_analysis.coordination import detect_coordination
from pickemup_analysis.domain import AccountObs, PostObs
from pickemup_analysis.graph import account_graph
from pickemup_analysis.propagation import group_narrative
from pickemup_connectors import BudgetGuard
from pickemup_connectors.file_imports import FileImportConnector
from pickemup_connectors.synthetic import SyntheticCampaignConnector
from pickemup_connectors.x_api import MockXConnector, XApiConnector
from pickemup_connectors.bluesky import BlueskyConnector

from .jobs import handler
from .models import Account, Alert, AuditLog, DetectionResult, Job, KV, Post, utcnow

class _ImportCsv(FileImportConnector):
    def __init__(self): super().__init__("csv")


class _ImportJsonl(FileImportConnector):
    def __init__(self): super().__init__("jsonl")


# values must be real classes: sources()/job gating reads the class attribute dev_only
CONNECTORS = {
    "synthetic_campaign": SyntheticCampaignConnector,
    "import_csv": _ImportCsv,
    "import_jsonl": _ImportJsonl,
    "x_api": XApiConnector,
    "x_api_mock": MockXConnector,
    "bluesky": BlueskyConnector,
}


def content_hash(kind: str, obj: dict) -> str:
    canon = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(f"{kind}:{canon}".encode("utf-8")).hexdigest()


def _to_domain(a: Account) -> AccountObs:
    return AccountObs(account_id=a.id, username=a.username, display_name=a.display_name,
                      created_at=a.created_at, followers=a.followers, following=a.following,
                      verified=a.verified, default_profile=a.default_profile,
                      origin_mode=a.origin_mode, ground_truth_label=a.gt_label)


def _p_to_domain(p: Post) -> PostObs:
    def aware(d):
        if d is not None and d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    return PostObs(post_id=p.id, author_id=p.author_id, text=p.text, created_at=aware(p.created_at),
                   collected_at=aware(p.collected_at), lang=p.lang, hashtags=list(p.hashtags or []),
                   urls=list(p.urls or []), mentions=list(p.mentions or []), repost_of=p.repost_of,
                   quote_of=p.quote_of, reply_to=p.reply_to, retweet_count=p.retweet_count,
                   origin_mode=p.origin_mode, source_url=p.source_url)


@handler("collect")
def job_collect(session: Session, job: Job) -> dict:
    kind = job.params.get("connector")
    if kind not in CONNECTORS:
        raise ValueError(f"unknown connector '{kind}' (available: {sorted(CONNECTORS)})")
    from .xcon import caps as x_caps, effective_token
    conn = CONNECTORS[kind](**({"token": effective_token(session)} if kind == "x_api" else {}))
    ok, why = conn.available()
    if not ok:
        raise RuntimeError(f"connector unavailable: {why}")
    per_job_cap, _ = x_caps(session)
    budget = BudgetGuard(
        per_job_usd=job.params.get("max_cost_usd", per_job_cap), per_day_usd=_daily_budget_left(session),
        post_read_usd=0.005, user_read_usd=0.010)
    batch = conn.run(job.params, budget)
    accounts_n = posts_n = dupes = 0
    for a in batch.accounts:
        ex = session.get(Account, a.account_id)
        if ex is None:
            session.add(Account(id=a.account_id, username=a.username, display_name=a.display_name,
                                created_at=a.created_at, followers=a.followers, following=a.following,
                                verified=a.verified, default_profile=a.default_profile,
                                origin_mode=batch.origin_mode, gt_label=a.ground_truth_label,
                                region=getattr(a, "region", None)))
            accounts_n += 1
        else:
            # backfill profile metadata learned on later runs (e.g. batched getProfile)
            for fld in ("created_at", "followers", "following", "verified", "default_profile"):
                nv = getattr(a, fld, None)
                if nv is not None and getattr(ex, fld, None) is None:
                    setattr(ex, fld, nv)
            if a.display_name and not ex.display_name:
                ex.display_name = a.display_name
    for p in batch.posts:
        if session.get(Post, p.post_id) is not None:
            dupes += 1
            continue
        chash = content_hash("post", {"id": p.post_id, "author": p.author_id, "text": p.text,
                                      "created_at": p.created_at.isoformat() if p.created_at else None})
        session.add(Post(id=p.post_id, author_id=p.author_id, text=p.text, created_at=p.created_at,
                         collected_at=p.collected_at, lang=p.lang, hashtags=p.hashtags, urls=p.urls,
                         mentions=p.mentions, repost_of=p.repost_of, quote_of=p.quote_of,
                         reply_to=p.reply_to, retweet_count=p.retweet_count,
                         origin_mode=batch.origin_mode, source_url=p.source_url,
                         content_hash=chash,
                         provenance={**batch.provenance, "job_id": job.id, "evidence_sha256": chash}))
        posts_n += 1
    _record_spend(session, budget)
    session.add(AuditLog(actor="worker", action="collect.ingest", subject=f"job:{job.id}",
                         detail={"connector": kind, "origin_mode": batch.origin_mode,
                                 "accounts_new": accounts_n, "posts_new": posts_n,
                                 "posts_deduped": dupes,
                                 "provenance": {k: str(v)[:200] for k, v in batch.provenance.items()}}))
    # entity reindex runs BEFORE the commit so the "one transaction" promise holds:
    # an index failure rolls ingest back too, instead of committing posts with a
    # half-rebuilt index. (reindex_entities flushes and commits the whole unit.)
    indexed = reindex_entities(session)  # keep the typed entity index fresh on ingest
    if posts_n and job.params.get("chain", True):
        # real collection must never sit unanalysed: queue the full engine chain
        # behind this job (idempotency keys stop pile-up on repeated collects;
        # demo/load chains itself and passes chain=False)
        from .jobs import enqueue as _enq
        for nk in ("analyze_automation", "analyze_content_risk", "analyze_coordination",
                   "analyze_narratives"):
            _enq(session, nk, {}, idempotency_key=f"chain:{job.id}:{nk}")
    return {"connector": kind, "origin_mode": batch.origin_mode, "accounts_new": accounts_n,
            "posts_new": posts_n, "posts_deduped": dupes, "entities_indexed": indexed,
            "spend_usd": round(budget.job_spent, 4), "mock": batch.origin_mode in ("mock", "synthetic"),
            # a zero-haul must never be silent: say WHY it stopped (budget cap etc.)
            **({"stopped_reason": batch.provenance["stopped_reason"]}
               if batch.provenance.get("stopped_reason") else {})}


def reindex_entities(session: Session) -> int:
    """Rebuild the typed entity index from stored post text (deterministic,
    fast at this corpus scale). Lineage kept per post for evidence pivots."""
    from pickemup_analysis.entities import extract_entities
    from .models import Entity, EntityPost
    posts = session.scalars(select(Post).where(Post.deleted.is_(False))).all()
    post_author = {p.id: p.author_id for p in posts}
    agg: dict[tuple[str, str], dict] = {}
    for p in posts:
        for etype, values in extract_entities(p.text or "").items():
            for v in values:
                if len(v) > 280:  # keep values inside the String(300) column everywhere
                    v = v[:280]
                key = (etype, v.lower())
                e = agg.setdefault(key, {"label": v, "posts": {}, "n": 0})
                e["posts"][p.id] = e["posts"].get(p.id, 0) + 1
                e["n"] += 1
                ts = p.created_at or p.collected_at
                if ts:
                    cur = e.get("first")
                    e["first"] = min(cur, ts) if cur else ts
                    cur2 = e.get("last")
                    e["last"] = max(cur2, ts) if cur2 else ts
    session.execute(delete(EntityPost))
    session.execute(delete(Entity))
    for (etype, value), d in agg.items():
        en = Entity(etype=etype, value=value, label=d["label"], post_count=len(d["posts"]),
                    first_seen=d.get("first"), last_seen=d.get("last"))
        session.add(en)
        session.flush()
        for pid, occ in d["posts"].items():
            session.add(EntityPost(entity_id=en.id, post_id=pid,
                                   author_id=post_author.get(pid, ""), occurrences=occ))
    session.add(AuditLog(actor="worker", action="entities.reindex", subject="index",
                         detail={"entities": len(agg), "posts": len(posts)}))
    session.commit()
    return len(agg)


@handler("extract_entities")
def job_extract_entities(session: Session, job: Job) -> dict:
    return {"entities": reindex_entities(session)}


@handler("analyze_automation")
def job_analyze_automation(session: Session, job: Job) -> dict:
    accounts = session.scalars(select(Account).where(Account.deleted.is_(False))).all()
    posts = session.scalars(select(Post).where(Post.deleted.is_(False))).all()
    by_acct: dict[str, list[PostObs]] = {}
    for p in posts:
        by_acct.setdefault(p.author_id, []).append(_p_to_domain(p))
    results = []
    for a in accounts:
        accts_posts = by_acct.get(a.id, [])
        cc = classify_posts([p.text for p in accts_posts])
        meta = {"default_profile": a.default_profile, "username": a.username,
                "display_name": a.display_name or ""}
        for k_attr, k_meta in (("created_at", "created_at"), ("followers", "followers"),
                               ("following", "following")):
            v = getattr(a, k_attr, None)
            if v is not None:
                meta[k_meta] = str(v) if k_attr == "created_at" else v
        r = score_account(a.id, accts_posts, meta, content_class=cc)
        results.append((a, r, cc))
    session.execute(delete(DetectionResult).where(DetectionResult.kind == "automation"))
    counts: dict[str, int] = {}
    for a, r, cc in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
        session.add(DetectionResult(
            kind="automation", subject_id=a.id, score=r.automation_score, verdict=r.verdict,
            confidence=r.confidence, algo_version=r.algo_version,
            payload={"signals": [{"name": s.name, "family": s.family, "value": s.value,
                                  "score": s.score, "detail": s.detail} for s in r.signals],
                     "evidence_post_ids": sorted(set(r.evidence_post_ids))[:25],
                     "content_class": cc,
                     "caveats": r.caveats, "account_origin_mode": a.origin_mode,
                     "weak_profile_signals": getattr(r, "weak_profile", [])}))
    session.add(AuditLog(actor="worker", action="analyze.automation", subject=f"job:{job.id}",
                         detail={"accounts": len(results), "verdicts": counts,
                                 "algo_version": ALGO_VERSION}))
    session.commit()
    return {"accounts_scored": len(results), "verdicts": counts}


@handler("analyze_content_risk")
def job_analyze_content_risk(session: Session, job: Job) -> dict:
    posts = session.scalars(select(Post).where(Post.deleted.is_(False))).all()
    # operator watchlist: DB is the source of truth, injected per run so the
    # engine itself stays pure/DB-free and new words apply corpus-wide.
    from .risk_lexicon import SEVERITY_WEIGHT, WATCHLIST_CAT, load_custom_terms
    custom = {t.term: (WATCHLIST_CAT, SEVERITY_WEIGHT[t.severity])
              for t in load_custom_terms(session)}
    lex_fp = hashlib.sha256("|".join(sorted(custom)).encode()).hexdigest()[:10] if custom else ""
    session.execute(delete(DetectionResult).where(DetectionResult.kind == "risk"))
    counts: dict[str, int] = {}
    high_reviewed = 0
    watch_hits = 0
    for p in posts:
        rr = risk_classify(p.text or "", has_media_only=bool(not (p.text or "").strip()),
                           custom=custom or None)
        if WATCHLIST_CAT in rr.matrix_categories:
            watch_hits += 1
        key = f"{rr.category}/{rr.severity}"
        counts[key] = counts.get(key, 0) + 1
        if rr.severity == "none" and rr.category == "benign":
            continue  # store only actionable classifications; benign is the default state
        conf_num = {"high": 0.9, "medium": 0.7, "low": 0.4, "insufficient": 0.1}.get(rr.confidence, 0.5)
        session.add(DetectionResult(
            kind="risk", subject_id=p.id, score=rr.score, verdict=rr.category,
            confidence=conf_num, algo_version=rr.algo_version,
            payload={**rr.to_dict(), "author_id": p.author_id, "excerpt": (p.text or "")[:220],
                     "post_origin_mode": p.origin_mode, "post_created_at": str(p.created_at)}))
        if rr.severity == "high":
            high_reviewed += 1
            _raise_alert(session, kind="content_risk",
                         severity="high" if rr.human_review_required else "medium",
                         dedupe=f"risk:{p.id}:{rr.algo_version}" + (f":{lex_fp}" if lex_fp else ""),
                         subject=p.id,
                         evidence={"category": rr.category, "score": rr.score, "language": rr.language,
                                   "matched_terms": rr.matched_terms[:5],
                                   "matrix_terms": rr.matched_matrix_terms[:5],
                                   "human_review_required": rr.human_review_required})
        elif rr.category == "watchlist_match":
            # a user-supplied trigger word fired on otherwise-clean content
            _raise_alert(session, kind="watchlist_match", severity="medium",
                         dedupe=f"watch:{p.id}:{lex_fp}", subject=p.author_id,
                         evidence={"post_id": p.id, "terms": rr.matched_matrix_terms[:8],
                                   "excerpt": (p.text or "")[:140]})
    session.add(AuditLog(actor="worker", action="analyze.content_risk", subject=f"job:{job.id}",
                         detail={"posts": len(posts), "flagged": sum(counts.values()),
                                 "high_severity": high_reviewed, "watchlist_hits": watch_hits,
                                 "watchlist_terms": len(custom), "algo_version": ALGO_VERSION}))
    session.commit()
    return {"posts_analyzed": len(posts), "flagged": sum(counts.values()), "high": high_reviewed,
            "watchlist_hits": watch_hits}


@handler("analyze_coordination")
def job_analyze_coordination(session: Session, job: Job) -> dict:
    accounts = [_to_domain(a) for a in session.scalars(
        select(Account).where(Account.deleted.is_(False))).all()]
    posts = [_p_to_domain(p) for p in session.scalars(select(Post).where(Post.deleted.is_(False))).all()]
    reports, edges, _acts = detect_coordination(accounts, posts)
    session.execute(delete(DetectionResult).where(DetectionResult.kind.in_(["cluster", "edge"])))
    for e in edges:
        session.add(DetectionResult(
            kind="edge", subject_id=f"{e.account_a}|{e.account_b}", score=max(e.signals.values(), default=0),
            verdict="coordination_edge", confidence=None, algo_version=ALGO_VERSION,
            payload={"signals": e.signals, "rule": e.rule, "why": e.why,
                     "evidence_post_ids": e.evidence_post_ids}))
    for r in reports:
        session.add(DetectionResult(
            kind="cluster", subject_id=r.cluster_id, score=r.confidence, verdict=r.verdict,
            confidence=r.confidence, algo_version=ALGO_VERSION,
            payload={"members": r.members, "accounts": r.accounts, "edge_count": r.edge_count,
                     "shared_domains": r.shared_domains, "shared_hashtags": r.shared_hashtags,
                     "signal_mix": r.signal_mix, "time_range": r.time_range,
                     "central_accounts": r.central_accounts, "amplifiers": r.amplifiers,
                     "organic_explanations": r.organic_explanations, "caveats": r.caveats}))
        if r.verdict != "insufficient_evidence" and r.accounts >= 5:
            _raise_alert(session, kind="new_coordination_cluster", severity="medium",
                         dedupe=f"cluster:{r.cluster_id}:{ALGO_VERSION}", subject=r.cluster_id,
                         evidence={"accounts": r.accounts, "signals": r.signal_mix,
                                   "confidence": r.confidence})
    session.add(AuditLog(actor="worker", action="analyze.coordination", subject=f"job:{job.id}",
                         detail={"edges": len(edges), "clusters": len(reports),
                                 "algo_version": ALGO_VERSION}))
    session.commit()
    return {"edges": len(edges), "clusters": len(reports)}


@handler("analyze_narratives")
def job_analyze_narratives(session: Session, job: Job) -> dict:
    q = job.params.get("query", "")
    posts_all = [_p_to_domain(p) for p in session.scalars(select(Post).where(Post.deleted.is_(False))).all()]
    qlist = [p for p in posts_all if q and (q.lower() in (p.text or "").lower()
                                            or any(q.lower() in h.lower() for h in p.hashtags))]
    started = session.scalar(select(KV).where(KV.key == "collection_start"))
    groups = group_narrative(qlist, corpus=posts_all,
                             collection_started=started.value.get("min_collected_at") if started else None)
    session.execute(delete(DetectionResult).where(DetectionResult.kind == "narrative"))
    for g in groups:
        session.add(DetectionResult(
            kind="narrative", subject_id=g.group_id, score=None,
            verdict="observed_only", confidence=0.5, algo_version=ALGO_VERSION,
            payload={"query": q, "post_ids": [p.post_id for p in g.posts],
                     "first_observed": {"post_id": g.first_observed.post_id,
                                        "author_id": g.first_observed.author_id,
                                        "at": g.first_observed.created_at.isoformat() if g.first_observed.created_at else None,
                                        "text": g.first_observed.text[:280]},
                     "domains": g.domains, "hashtags": g.hashtags, "cascade": g.cascade[:400],
                     "amplifiers": g.amplifiers, "coverage_gaps": g.coverage_gaps, "caveats": g.caveats}))
    session.commit()
    return {"query": q, "groups": len(groups),
            "posts": sum(len(g.posts) for g in groups)}


def build_graph_snapshot(session: Session):
    accounts = [_to_domain(a) for a in session.scalars(
        select(Account).where(Account.deleted.is_(False))).all()]
    posts = [_p_to_domain(p) for p in session.scalars(select(Post).where(Post.deleted.is_(False))).all()]
    from dataclasses import dataclass as dc
    from pickemup_analysis.coordination import PairEvidence, ClusterReport
    edges = []
    for row in session.scalars(select(DetectionResult).where(DetectionResult.kind == "edge")).all():
        a, b = row.subject_id.split("|", 1)
        edges.append(PairEvidence(a, b, row.payload.get("signals", {}),
                                  row.payload.get("evidence_post_ids", []), True,
                                  row.payload.get("rule"), row.payload.get("why", "")))
    reports = []
    for row in session.scalars(select(DetectionResult).where(DetectionResult.kind == "cluster")).all():
        pl = row.payload
        reports.append(ClusterReport(cluster_id=row.subject_id, members=pl["members"],
                                     accounts=pl["accounts"], edge_count=pl["edge_count"],
                                     shared_domains=pl.get("shared_domains", pl.get("shared_urls", [])),
                                     shared_hashtags=pl["shared_hashtags"],
                                     signal_mix=pl["signal_mix"], time_range=pl["time_range"],
                                     central_accounts=pl["central_accounts"], amplifiers=pl["amplifiers"],
                                     organic_explanations=pl["organic_explanations"],
                                     confidence=row.confidence or 0, verdict=row.verdict or "",
                                     caveats=pl.get("caveats", [])))
    snap = account_graph(accounts, posts, edges, reports).to_dict()
    # UI-facing "what is this cluster" explanations — straight from stored engine
    # payloads (no re-derivation, same provenance as the campaigns page).
    unames = {a.id: a.username for a in session.scalars(select(Account)).all()}
    meta = {}
    for row in session.scalars(select(DetectionResult).where(DetectionResult.kind == "cluster")).all():
        pl = row.payload
        meta[row.subject_id] = {
            "verdict": row.verdict, "confidence": round(row.confidence or 0, 3),
            "accounts": pl.get("accounts"), "edges": pl.get("edge_count"),
            "signal_mix": pl.get("signal_mix"), "shared_hashtags": (pl.get("shared_hashtags") or [])[:6],
            "shared_domains": (pl.get("shared_domains") or pl.get("shared_urls") or [])[:4],
            "central_accounts": [unames.get(m, m) for m in (pl.get("central_accounts") or [])[:5]],
            "amplifiers": [unames.get(m, m) for m in (pl.get("amplifiers") or [])[:5]],
            "organic_explanations": pl.get("organic_explanations") or [],
            "caveats": pl.get("caveats") or [], "time_range": pl.get("time_range")}
    snap["cluster_meta"] = meta
    return snap


# ---- helpers ----

def _daily_budget_left(session: Session) -> float:
    from .xcon import caps as x_caps
    _, per_day = x_caps(session)
    kv = session.get(KV, "budget")
    today = utcnow().strftime("%Y-%m-%d")
    spent = kv.value.get(today, 0.0) if kv else 0.0
    return max(0.0, per_day - float(spent))


def _record_spend(session: Session, budget: BudgetGuard) -> None:
    if budget.job_spent <= 0:
        return
    # row lock so concurrent worker threads/replicas cannot lose updates in the
    # read-modify-write (SQLite ignores FOR UPDATE but serializes writers anyway)
    kv = session.scalar(select(KV).where(KV.key == "budget").with_for_update())
    today = utcnow().strftime("%Y-%m-%d")
    val = dict(kv.value) if kv else {}
    val[today] = round(float(val.get(today, 0.0)) + budget.job_spent, 6)
    if kv:
        kv.value = val
    else:
        session.add(KV(key="budget", value=val))
    session.commit()


def _raise_alert(session: Session, kind: str, severity: str, dedupe: str, subject: str,
                 evidence: dict) -> None:
    """Dedup-by-key: alert storms are suppressed (directive §16)."""
    exists = session.scalar(select(Alert).where(Alert.dedupe_key == dedupe))
    if exists:
        return
    try:
        with session.begin_nested():  # dedupe race with another worker thread is
            session.add(Alert(kind=kind, severity=severity, dedupe_key=dedupe,  # suppression, not a job failure
                              subject_id=subject, evidence=evidence))
    except IntegrityError:
        return
