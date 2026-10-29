"""PICKEMUP HTTP API (FastAPI). Local-first; optional API key via env."""
from __future__ import annotations

import html
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import hashlib
import hmac
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import __version__
from .config import settings
from .db import get_session, init_db
from .jobs import Worker, enqueue
from .llm import LLMError, get_config, public_status, save_config, validate_base_url, probe as llm_probe
from .models import (Account, Alert, AuditLog, Case, CaseMember, CaseNote, DetectionResult,
                     Job, KV, Post, Report, ReportDelivery, utcnow)
from .pipeline import build_graph_snapshot
from . import monitor as monitor_mod
from .ai_routes import router as ai_router
from .lexicon_routes import router as lexicon_router
from .search_routes import router as search_router
from .xcon import PURGE_ROUTER as x_purge_router
from .xcon import router as xcon_router
from .correlate import router as correlations_router

worker: Worker | None = None
monitor_thread: monitor_mod.MonitorScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker, monitor_thread
    init_db()
    with next(get_session()) as s:
        if s.get(KV, "collection_start") is None:
            s.add(KV(key="collection_start", value={"min_collected_at": utcnow().isoformat()}))
            s.commit()
    if settings.pickemup_env != "test":
        worker = Worker(threads=settings.pickemup_worker_threads,
                        poll_seconds=settings.pickemup_worker_poll_seconds)
        worker.start()
        monitor_thread = monitor_mod.MonitorScheduler()
        monitor_thread.start()
    yield
    if monitor_thread:
        monitor_thread.stop()
    if worker:
        worker.stop()


app = FastAPI(title="PICKEMUP API", version=__version__, lifespan=lifespan)
app.include_router(ai_router)
app.include_router(lexicon_router)
app.include_router(search_router)
app.include_router(correlations_router)
app.include_router(xcon_router)
app.include_router(x_purge_router)
# dev origins: the standard :3000 slot can be squatted by OAuth-callback listeners
# on this machine, so :3001 is also allowed for local dev
@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    if (settings.pickemup_api_key and request.url.path.startswith("/api/")
            and not hmac.compare_digest(request.headers.get("x-api-key", ""),
                                        settings.pickemup_api_key)):
        return JSONResponse({"detail": "invalid API key"}, status_code=401,
                            headers={"WWW-Authenticate": "ApiKey"})
    return await call_next(request)


# added AFTER api_key_guard on purpose: last-added middleware runs outermost, so
# CORS preflights succeed and even guard 401s carry CORS headers for keyed clients.
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000",
                  "http://127.0.0.1:3000", "http://localhost:3001",
                  "http://127.0.0.1:3001"],
                   allow_methods=["*"], allow_headers=["*"])


def session_dep():
    yield from get_session()


def _j(row: Job) -> dict:
    return {"id": row.id, "kind": row.kind, "params": row.params, "status": row.status,
            "attempts": row.attempts, "error": row.error, "result": row.result,
            "created_at": row.created_at.isoformat(), "finished_at":
            row.finished_at.isoformat() if row.finished_at else None}


# ---------- health & overview ----------

@app.get("/health")
def health(s: Session = Depends(session_dep)):
    def n(model):
        return s.scalar(select(func.count()).select_from(model))
    kv = s.get(KV, "worker")
    hb = kv.value if kv else None
    db_ms = 0
    db_ok = True
    try:
        t = datetime.now(timezone.utc)
        s.scalar(select(1))
        db_ms = round((datetime.now(timezone.utc) - t).total_seconds() * 1000, 1)
    except Exception:
        db_ok = False
        db_ms = -1
    return {"status": "ok" if db_ok else "degraded", "version": __version__, "env": settings.pickemup_env,
            "db": {"reachable": db_ok, "latency_ms": db_ms if db_ok else None, "backend":
                   "sqlite" if settings.is_sqlite else "postgres"},
            "worker": {"alive": bool(hb), "last_heartbeat": (hb or {}).get("ts"),
                       "worker_id": (hb or {}).get("worker_id")},
            "counts": {"accounts": n(Account), "posts": n(Post), "jobs": n(Job),
                       "detections": n(DetectionResult), "cases": n(Case), "alerts": n(Alert)}}


@app.get("/api/v1/overview")
def overview(s: Session = Depends(session_dep)):
    def by_origin(model):
        rows = s.execute(select(model.origin_mode if hasattr(model, "origin_mode") else model.kind,
                                 func.count()).group_by(
            model.origin_mode if hasattr(model, "origin_mode") else model.kind)).all()
        return {r[0]: r[1] for r in rows}
    clusters = s.scalars(select(DetectionResult).where(DetectionResult.kind == "cluster")
                         .order_by(DetectionResult.confidence.desc()).limit(5)).all()
    recent_jobs = s.scalars(select(Job).order_by(Job.id.desc()).limit(8)).all()
    budget = s.get(KV, "budget")
    # Threat KPI derived ONLY from stored risk detections (directive §25: no fabricated telemetry)
    risk_rows = s.scalars(select(DetectionResult).where(DetectionResult.kind == "risk")).all()
    by_cat: dict[str, int] = {}
    for d in risk_rows:
        by_cat[d.verdict] = by_cat.get(d.verdict, 0) + 1
    threats_high = sum(1 for d in risk_rows if d.payload.get("severity") == "high")
    threat_level = ("critical" if threats_high >= 10 else "high" if threats_high >= 3
                    else "moderate" if threats_high >= 1 else "low")
    return {"accounts_by_origin": by_origin(Account), "posts_by_origin": by_origin(Post),
            "risk": {"flagged": len(risk_rows), "high_severity": threats_high,
                     "by_category": by_cat, "threat_level": threat_level},
            "top_clusters": [{"id": c.subject_id, "accounts": c.payload.get("accounts"),
                              "verdict": c.verdict, "confidence": c.confidence,
                              "signals": c.payload.get("signal_mix")} for c in clusters],
            "recent_jobs": [_j(j) for j in recent_jobs],
            "api_spend_usd_by_day": (budget.value if budget else {}),
            "data_freshness": {"newest_post": None},
            "labels_note": "origin_mode: live|fixture|mock|synthetic — synthetic/mock data is demo data"}


@app.get("/api/v1/analysis/risk")
def analysis_risk(severity: str | None = None, category: str | None = None,
                  limit: int = 100, s: Session = Depends(session_dep)):
    q = select(DetectionResult).where(DetectionResult.kind == "risk")
    # filters go to SQL BEFORE the limit — python-side filtering after a fixed
    # fetch silently truncated results and under-counted matches
    if category:
        q = q.where(DetectionResult.verdict == category)
    if severity:
        q = q.where(DetectionResult.payload["severity"].as_string() == severity)
    total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
    limit = max(1, min(int(limit), 500))
    rows = s.scalars(q.order_by(DetectionResult.score.desc()).limit(limit)).all()
    handles = {}
    aids = {str(d.payload.get("author_id") or "") for d in rows} - {""}
    if aids:
        handles = {x.id: x.username for x in s.scalars(select(Account).where(Account.id.in_(aids))).all()}
    out = [{"post_id": d.subject_id, "category": d.verdict, "score": d.score,
            "author_handle": handles.get(str(d.payload.get("author_id") or "")),
            "severity": d.payload.get("severity"), "language": d.payload.get("language"),
            "confidence": d.confidence, "matched_terms": d.payload.get("matched_terms"),
            "caveats": d.payload.get("caveats"), "excerpt": d.payload.get("excerpt"),
            "author_id": d.payload.get("author_id"), "origin_mode": d.payload.get("post_origin_mode"),
            "human_review_required": d.payload.get("human_review_required"),
            "moderation": d.payload.get("moderation"),
            "algo_version": d.algo_version}
           for d in rows]
    return {"count": total, "items": out,
            "note": "bilingual lexicon+centroid triage classifier (mvp): a prioritization signal, "
                    "not a determination of intent or affiliation; high severity requires analyst review"}


# ---------- geo intelligence (offline; macro-region centroids only) ----------

_REGION_CENTERS = {
    "NA_EAST": (38.9, -77.0), "LATAM": (-15.8, -47.9), "NW_EU": (51.5, -0.1),
    "E_EU": (50.4, 30.5), "LEVANT": (33.9, 35.5), "GULF": (25.3, 51.2),
    "MAGHREB": (31.8, -3.7), "SAHEL": (12.0, 8.5), "WAFRICA": (6.5, 3.4),
    "SASIA": (28.6, 77.2), "SEA": (1.35, 103.8), "EASIA": (35.7, 139.7),
    "OCEANIA": (-33.9, 151.2),
}


@app.get("/api/v1/geo")
def geo(s: Session = Depends(session_dep)):
    accounts = s.scalars(select(Account).where(Account.deleted.is_(False))).all()
    by_region: dict[str, dict] = {}
    for a in accounts:
        if not a.region:
            continue
        m = by_region.setdefault(a.region, {"code": a.region, "accounts": 0, "automated": 0,
                                            "high_risk_posts": 0, "clusters": set()})
        m["accounts"] += 1
    auto_ids = {d.subject_id for d in
                s.scalars(select(DetectionResult).where(DetectionResult.kind == "automation",
                        DetectionResult.verdict.in_(["likely_automated", "mixed_automation"]))).all()}
    risk_authors: dict[str, int] = {}
    for d in s.scalars(select(DetectionResult).where(DetectionResult.kind == "risk",
                    DetectionResult.payload["severity"].as_string() == "high")).all():
        aid = d.payload.get("author_id")
        risk_authors[aid] = risk_authors.get(aid, 0) + 1
    acct_region = {a.id: a.region for a in accounts}
    for a in accounts:
        if a.id in auto_ids:
            m = by_region.get(a.region)
            if m:
                m["automated"] += 1
        if a.region and a.id in risk_authors:
            by_region[a.region]["high_risk_posts"] += risk_authors[a.id]
    for c in s.scalars(select(DetectionResult).where(DetectionResult.kind == "cluster")).all():
        members = c.payload.get("members") or []
        tally: dict[str, int] = {}
        for mid in members:
            r = acct_region.get(mid)
            if r:
                tally[r] = tally.get(r, 0) + 1
        if tally:
            top = max(tally, key=lambda k: tally[k])
            if tally[top] >= 3:
                by_region.setdefault(top, {"code": top, "accounts": 0, "automated": 0,
                                           "high_risk_posts": 0, "clusters": set()})
                by_region[top]["clusters"].add(c.subject_id)
    out = []
    for code, m in by_region.items():
        if code not in _REGION_CENTERS:
            continue  # unmapped connector region — skip, never 500 the whole map
        lat, lon = _REGION_CENTERS[code]
        out.append({"code": code, "lat": lat, "lon": lon, "accounts": m["accounts"],
                    "automated": m["automated"], "high_risk_posts": m["high_risk_posts"],
                    "clusters": sorted(m["clusters"])})
    return {"markers": sorted(out, key=lambda x: -(x["high_risk_posts"] * 3 + x["automated"] + x["accounts"] * 0.1)),
            "note": "Macro-region centroids only. Regions are ESTIMATED (language/profile heuristics or "
                    "synthetic ground truth) — never precise geolocation; X posts rarely carry geo-tags. "
                    "No map tiles are fetched: rendering is fully offline by design (opsec)."}


@app.get("/api/v1/geo/interactions")
def geo_interactions(s: Session = Depends(session_dep)):
    """Directed region-level interaction arcs (actor -> target) derived ONLY from
    collected repost/quote/mention records where at least one endpoint is a
    flagged account (automation-flagged or high-severity risk author)."""
    accounts = {a.id: a for a in s.scalars(select(Account).where(Account.deleted.is_(False))).all()}
    by_username: dict[str, Account] = {}
    for a in accounts.values():
        if a.username:
            by_username[a.username.lower()] = a
    auto_ids = {d.subject_id for d in s.scalars(select(DetectionResult).where(
        DetectionResult.kind == "automation",
        DetectionResult.verdict.in_(["likely_automated", "mixed_automation"]))).all()}
    posts = s.scalars(select(Post)).all()
    post_by_id = {p.id: p for p in posts}
    flagged = set(auto_ids)
    for d in s.scalars(select(DetectionResult).where(
            DetectionResult.kind == "risk",
            DetectionResult.payload["severity"].as_string() == "high")).all():
        p = post_by_id.get(str(d.subject_id))
        if p:
            flagged.add(p.author_id)
    tally: dict[tuple, int] = {}
    for p in posts:
        src = accounts.get(p.author_id)
        if not src or not src.region:
            continue
        targets: list[tuple[str, str]] = []
        for kind, ref in (("repost", p.repost_of), ("quote", p.quote_of), ("reply", p.reply_to)):
            if ref:
                parent = post_by_id.get(ref)
                if parent:
                    targets.append((kind, parent.author_id))
        for m in (p.mentions or []):
            u = str(m).lstrip("@").strip().lower()
            tgt = by_username.get(u)
            if tgt:
                targets.append(("mention", tgt.id))
        for kind, tid in targets:
            dst = accounts.get(tid)
            if not dst or not dst.region or dst.id == src.id:
                continue
            f_src, f_dst = src.id in flagged, dst.id in flagged
            if not (f_src or f_dst):
                continue
            key = (src.region, dst.region, kind, "both" if f_src and f_dst else "one")
            tally[key] = tally.get(key, 0) + 1
    links = [{"from": k[0], "to": k[1], "kind": k[2], "flagged": k[3], "count": v}
             for k, v in tally.items()]
    links.sort(key=lambda l: -l["count"])
    return {"links": links[:40], "flagged_accounts": len(flagged),
            "note": "Arcs are OBSERVED interactions (repost/mention records) aggregated to macro-region "
                    "centroids; direction = actor who reposted/mentioned -> target. Regions are estimated; "
                    "an arc does not imply the accounts are physically at those points."}


# ---------- sources & jobs ----------

@app.get("/api/v1/sources")
def sources(s: Session = Depends(session_dep)):
    from .pipeline import CONNECTORS
    from .xcon import effective_token
    from .config import settings
    out = {}
    for name, ctor in CONNECTORS.items():
        if ctor.dev_only and not settings.dev_tools:
            continue  # production console: operational sources only
        # x_api availability must reflect the token saved via the UI, not only env
        c = ctor(**({"token": effective_token(s)} if name == "x_api" else {}))
        ok, why = c.available()
        m = c.manifest
        out[name] = {"available": ok, "reason": why, "platform": m.platform,
                     "capabilities": m.capabilities, "auth_required": m.auth_required,
                     "rate_limit": m.rate_limit_desc, "cost_model": m.cost_model,
                     "coverage_gaps": m.coverage_gaps, "retention": m.retention_rule,
                     "freshness": m.data_freshness}
    return {"connectors": out, "dev_tools": settings.dev_tools}


class JobIn(BaseModel):
    kind: str
    params: dict = Field(default_factory=dict)
    idempotency_key: str | None = None


@app.post("/api/v1/jobs", status_code=201)
def create_job(body: JobIn, s: Session = Depends(session_dep)):
    kind, params = body.kind, dict(body.params or {})
    from .config import settings
    from .pipeline import CONNECTORS
    if kind in CONNECTORS:  # connector-name shortcut: {kind:"x_api"} == collect + params.connector
        params.setdefault("connector", kind)
        kind = "collect"
    conn = params.get("connector") if kind == "collect" else None
    if conn in CONNECTORS and CONNECTORS[conn].dev_only and not settings.dev_tools:
        raise HTTPException(404, f"connector '{conn}' is a dev/test surface and is disabled")
    j = enqueue(s, kind, params, body.idempotency_key)
    return _j(j)


@app.get("/api/v1/jobs")
def list_jobs(status: str | None = None, limit: int = 50, s: Session = Depends(session_dep)):
    q = select(Job).order_by(Job.id.desc()).limit(min(limit, 200))
    if status:
        q = select(Job).where(Job.status == status).order_by(Job.id.desc()).limit(min(limit, 200))
    return [_j(r) for r in s.scalars(q).all()]


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: int, s: Session = Depends(session_dep)):
    j = s.get(Job, job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return _j(j)


@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: int, s: Session = Depends(session_dep)):
    j = s.get(Job, job_id)
    if not j:
        raise HTTPException(404, "job not found")
    if j.status in ("done", "dead"):
        raise HTTPException(409, f"job already {j.status}")
    j.status = "cancelled"
    s.commit()
    return _j(j)


class DemoIn(BaseModel):
    seed: int = 1337


@app.post("/api/v1/demo/load")
def demo_load(body: DemoIn, s: Session = Depends(session_dep)):
    """One-shot demo pipeline: synthetic collection -> automation -> coordination -> narratives."""
    from .config import settings
    if not settings.dev_tools:
        raise HTTPException(404, "demo dataset disabled (dev tools off)")
    jobs = [enqueue(s, "collect", {"connector": "synthetic_campaign", "seed": body.seed,
                                    "chain": False}),
            enqueue(s, "extract_entities", {}),
            enqueue(s, "analyze_automation", {}),
            enqueue(s, "analyze_content_risk", {}),
            enqueue(s, "analyze_coordination", {}),
            enqueue(s, "analyze_narratives", {"query": "synthetic-rally"})]
    return {"enqueued": [_j(j) for j in jobs]}


# ---------- accounts & posts ----------

@app.get("/api/v1/accounts")
def list_accounts(q: str | None = None, origin: str | None = None, verdict: str | None = None,
                  limit: int = 50, offset: int = 0, s: Session = Depends(session_dep)):
    qs = select(Account, DetectionResult.verdict, DetectionResult.score, DetectionResult.confidence)\
        .outerjoin(DetectionResult, (DetectionResult.subject_id == Account.id)
                   & (DetectionResult.kind == "automation")).where(Account.deleted.is_(False))
    if q:
        qs = qs.where(or_(Account.username.ilike(f"%{q}%"), Account.display_name.ilike(f"%{q}%")))
    if origin:
        qs = qs.where(Account.origin_mode == origin)
    if verdict:
        qs = qs.where(DetectionResult.verdict == verdict)
    rows = s.execute(qs.order_by(DetectionResult.score.desc().nullslast()).offset(offset)
                     .limit(min(limit, 200))).all()
    return [{"account_id": a.id, "username": a.username, "display_name": a.display_name,
             "origin_mode": a.origin_mode, "automation_verdict": v, "automation_score": sc,
             "confidence": cf} for a, v, sc, cf in rows]


@app.get("/api/v1/accounts/{account_id}")
def account_detail(account_id: str, s: Session = Depends(session_dep)):
    a = s.get(Account, account_id)
    if not a:
        raise HTTPException(404, "account not found")
    det = s.scalar(select(DetectionResult).where(DetectionResult.kind == "automation",
                                                 DetectionResult.subject_id == account_id))
    posts = s.scalars(select(Post).where(Post.author_id == account_id, Post.deleted.is_(False))
                      .order_by(Post.created_at.desc().nullslast()).limit(30)).all()
    memberships = []
    for c in s.scalars(select(DetectionResult).where(DetectionResult.kind == "cluster")).all():
        if account_id in c.payload.get("members", []):
            memberships.append({"cluster": c.subject_id, "verdict": c.verdict,
                                "confidence": c.confidence})
    return {"account_id": a.id, "username": a.username, "display_name": a.display_name,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "followers": a.followers, "following": a.following, "verified": a.verified,
            "default_profile": a.default_profile, "origin_mode": a.origin_mode,
            "ground_truth_label": a.gt_label,
            "automation": ({"verdict": det.verdict, "score": det.score, "confidence": det.confidence,
                            **det.payload} if det else {"verdict": "not_analyzed"}),
            "cluster_memberships": memberships,
            "recent_posts": [{"post_id": p.id, "text": p.text[:280],
                              "created_at": p.created_at.isoformat() if p.created_at else None,
                              "hashtags": p.hashtags, "urls": p.urls, "repost_of": p.repost_of,
                              "source_url": p.source_url, "content_sha256": p.content_hash}
                             for p in posts]}


@app.get("/api/v1/posts")
def list_posts(q: str | None = None, hashtag: str | None = None, domain: str | None = None,
               author: str | None = None, limit: int = 50, offset: int = 0,
               s: Session = Depends(session_dep)):
    qs = select(Post).where(Post.deleted.is_(False))
    if q:
        qs = qs.where(Post.text.ilike(f"%{q}%"))
    if author:
        qs = qs.where(Post.author_id == author)
    rows = s.scalars(qs.order_by(Post.created_at.desc().nullslast()).offset(offset)
                     .limit(min(limit, 200))).all()
    if hashtag:
        rows = [r for r in rows if any(hashtag.lower().lstrip("#") in h.lower() for h in r.hashtags)]
    if domain:
        rows = [r for r in rows if any(domain.lower() in u.lower() for u in r.urls)]
    return [{"post_id": r.id, "author_id": r.author_id, "text": r.text[:280],
             "created_at": r.created_at.isoformat() if r.created_at else None,
             "hashtags": r.hashtags, "urls": r.urls, "repost_of": r.repost_of,
             "quote_of": r.quote_of, "origin_mode": r.origin_mode, "source_url": r.source_url,
             "content_sha256": r.content_hash} for r in rows]


@app.get("/api/v1/posts/{post_id}/evidence")
def post_evidence(post_id: str, s: Session = Depends(session_dep)):
    p = s.get(Post, post_id)
    if not p:
        raise HTTPException(404, "post not found")
    return {"post_id": p.id, "author_id": p.author_id, "text": p.text,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "collected_at": p.collected_at.isoformat() if p.collected_at else None,
            "content_sha256": p.content_hash, "source_url": p.source_url,
            "provenance": p.provenance, "origin_mode": p.origin_mode, "deleted": p.deleted}


# ---------- analysis ----------

@app.get("/api/v1/analysis/coordination")
def coordination(s: Session = Depends(session_dep)):
    rows = s.scalars(select(DetectionResult).where(DetectionResult.kind == "cluster")
                     .order_by(DetectionResult.confidence.desc())).all()
    return {"algo_version": rows[0].algo_version if rows else None,
            "clusters": [{"cluster_id": r.subject_id, "verdict": r.verdict,
                          "confidence": r.confidence, **r.payload} for r in rows]}


@app.get("/api/v1/analysis/graph")
def graph(s: Session = Depends(session_dep)):
    return build_graph_snapshot(s)


class NarrativeIn(BaseModel):
    query: str


@app.post("/api/v1/analysis/narratives")
def narratives_run(body: NarrativeIn, s: Session = Depends(session_dep)):
    return _j(enqueue(s, "analyze_narratives", {"query": body.query}))


@app.get("/api/v1/analysis/narratives")
def narratives_read(s: Session = Depends(session_dep)):
    rows = s.scalars(select(DetectionResult).where(DetectionResult.kind == "narrative")
                     .order_by(DetectionResult.id.desc())).all()
    return [{"narrative_id": r.subject_id, "query": r.payload.get("query"),
             "first_observed": r.payload.get("first_observed"),
             "post_count": len(r.payload.get("post_ids", [])),
             "domains": r.payload.get("domains"), "hashtags": r.payload.get("hashtags"),
             "amplifiers": r.payload.get("amplifiers"), "cascade": r.payload.get("cascade"),
             "coverage_gaps": r.payload.get("coverage_gaps"), "caveats": r.payload.get("caveats"),
             "algo_version": r.algo_version} for r in rows]


# ---------- cases ----------

class CaseIn(BaseModel):
    title: str
    scope: str = ""
    analyst: str = "analyst"


class MemberIn(BaseModel):
    kind: str            # account|post|cluster|narrative
    ref_id: str


class NoteIn(BaseModel):
    text: str
    author: str = "analyst"


class FindingsIn(BaseModel):
    findings: str
    status: str | None = None


@app.post("/api/v1/cases", status_code=201)
def create_case(body: CaseIn, s: Session = Depends(session_dep)):
    cid = "CASE-" + utcnow().strftime("%Y%m%d") + "-" + hashlib.sha256(
        (body.title + str(utcnow())).encode()).hexdigest()[:8].upper()
    c = Case(id=cid, title=body.title, scope=body.scope, analyst=body.analyst)
    s.add(c)
    s.add(AuditLog(actor="analyst", action="case.create", subject=cid, detail={"title": body.title}))
    s.commit()
    return {"case_id": cid}


@app.get("/api/v1/cases")
def list_cases(s: Session = Depends(session_dep)):
    return [{"case_id": c.id, "title": c.title, "status": c.status, "analyst": c.analyst,
             "created_at": c.created_at.isoformat()} for c in s.scalars(select(Case)).all()]


@app.get("/api/v1/cases/{case_id}")
def case_detail(case_id: str, s: Session = Depends(session_dep)):
    c = s.get(Case, case_id)
    if not c:
        raise HTTPException(404, "case not found")
    members = s.scalars(select(CaseMember).where(CaseMember.case_id == case_id)).all()
    notes = s.scalars(select(CaseNote).where(CaseNote.case_id == case_id)
                      .order_by(CaseNote.id)).all()
    audit = s.scalars(select(AuditLog).where(AuditLog.subject == case_id).order_by(AuditLog.id)).all()
    return {"case_id": c.id, "title": c.title, "scope": c.scope, "status": c.status,
            "analyst": c.analyst, "findings": c.findings,
            "members": [{"kind": m.kind, "ref_id": m.ref_id, "added_at": m.added_at.isoformat()}
                        for m in members],
            "notes": [{"author": n.author, "text": n.text, "at": n.created_at.isoformat()}
                      for n in notes],
            "audit": [{"ts": a.ts.isoformat(), "actor": a.actor, "action": a.action,
                       "detail": a.detail} for a in audit]}


@app.post("/api/v1/cases/{case_id}/members")
def add_member(case_id: str, body: MemberIn, s: Session = Depends(session_dep)):
    if not s.get(Case, case_id):
        raise HTTPException(404, "case not found")
    if body.kind not in ("account", "post", "cluster", "narrative"):
        raise HTTPException(422, "bad member kind")
    s.add(CaseMember(case_id=case_id, kind=body.kind, ref_id=body.ref_id))
    s.add(AuditLog(actor="analyst", action="case.add_member", subject=case_id,
                   detail={"kind": body.kind, "ref": body.ref_id}))
    s.commit()
    return {"ok": True}


@app.post("/api/v1/cases/{case_id}/notes")
def add_note(case_id: str, body: NoteIn, s: Session = Depends(session_dep)):
    if not s.get(Case, case_id):
        raise HTTPException(404, "case not found")
    s.add(CaseNote(case_id=case_id, text=body.text, author=body.author))
    s.add(AuditLog(actor=body.author, action="case.note", subject=case_id,
                   detail={"chars": len(body.text)}))
    s.commit()
    return {"ok": True}


@app.put("/api/v1/cases/{case_id}")
def update_case(case_id: str, body: FindingsIn, s: Session = Depends(session_dep)):
    c = s.get(Case, case_id)
    if not c:
        raise HTTPException(404, "case not found")
    c.findings = body.findings
    if body.status:
        if body.status not in ("open", "review", "closed"):
            raise HTTPException(422, "bad status")
        c.status = body.status
    s.add(AuditLog(actor="analyst", action="case.update", subject=case_id,
                   detail={"status": c.status}))
    s.commit()
    return {"ok": True}


def _case_report_data(case_id: str, s: Session) -> dict:
    c = s.get(Case, case_id)
    if not c:
        raise HTTPException(404, "case not found")
    members = s.scalars(select(CaseMember).where(CaseMember.case_id == case_id)).all()
    evidence = []
    for m in members:
        if m.kind == "post":
            p = s.get(Post, m.ref_id)
            if p:
                evidence.append({"kind": "post", "id": p.id, "sha256": p.content_hash,
                                 "source_url": p.source_url, "origin_mode": p.origin_mode,
                                 "excerpt": p.text[:200], "collected_at":
                                 p.collected_at.isoformat() if p.collected_at else None})
        elif m.kind == "account":
            a = s.get(Account, m.ref_id)
            if a:
                det = s.scalar(select(DetectionResult).where(DetectionResult.kind == "automation",
                                                             DetectionResult.subject_id == a.id))
                evidence.append({"kind": "account", "id": a.id, "username": a.username,
                                 "origin_mode": a.origin_mode,
                                 "automation": {"verdict": det.verdict, "score": det.score} if det else None})
        elif m.kind == "cluster":
            r = s.scalar(select(DetectionResult).where(DetectionResult.kind == "cluster",
                                                       DetectionResult.subject_id == m.ref_id))
            if r:
                evidence.append({"kind": "cluster", "id": r.subject_id, "verdict": r.verdict,
                                 "confidence": r.confidence, **{k: r.payload.get(k) for k in
                                    ("accounts", "signal_mix", "organic_explanations", "caveats")}})
        elif m.kind == "narrative":
            r = s.scalar(select(DetectionResult).where(DetectionResult.kind == "narrative",
                                                       DetectionResult.subject_id == m.ref_id))
            if r:
                evidence.append({"kind": "narrative", "id": r.subject_id,
                                 "first_observed": r.payload.get("first_observed"),
                                 "coverage_gaps": r.payload.get("coverage_gaps")})
    notes = s.scalars(select(CaseNote).where(CaseNote.case_id == case_id).order_by(CaseNote.id)).all()
    manifest = hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest()
    return {"case_id": c.id, "title": c.title, "scope": c.scope, "status": c.status,
            "analyst": c.analyst, "findings": c.findings,
            "notes": [{"author": n.author, "text": n.text, "at": n.created_at.isoformat()} for n in notes],
            "evidence": evidence, "generated_at": utcnow().isoformat(),
            "evidence_manifest_sha256": manifest,
            "disclaimer": ("Findings are analytical inferences from observed records, not proof of "
                           "identity, affiliation, or intent. 'Earliest observed' is not 'original' "
                           "when collection history is incomplete.")}


@app.get("/api/v1/cases/{case_id}/report")
def case_report(case_id: str, s: Session = Depends(session_dep)):
    d = _case_report_data(case_id, s)
    s.add(AuditLog(actor="analyst", action="case.export", subject=case_id,
                   detail={"manifest": d["evidence_manifest_sha256"][:16]}))
    s.commit()
    return d


@app.get("/api/v1/cases/{case_id}/report.html", response_class=HTMLResponse)
def case_report_html(case_id: str, s: Session = Depends(session_dep)):
    d = _case_report_data(case_id, s)
    e = html.escape
    rows = []
    for it in d["evidence"]:
        rows.append(f"<tr><td>{e(it['kind'])}</td><td>{e(str(it['id']))}</td>"
                    f"<td><code>{e(str(it.get('sha256') or it.get('confidence') or ''))[:24]}</code></td>"
                    f"<td>{e(str(it.get('source_url') or it.get('username') or it.get('verdict') or ''))}</td>"
                    f"<td>{e(str(it.get('excerpt') or it.get('automation') or ''))[:160]}</td>"
                    f"<td>{e(str(it.get('origin_mode') or ''))}</td></tr>")
    notes = "".join(f"<li><b>{e(n['author'])}</b> ({e(n['at'][:19])}): {e(n['text'])}</li>"
                    for n in d["notes"])
    s.add(AuditLog(actor="analyst", action="case.export_html", subject=case_id, detail={}))
    s.commit()
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(d['case_id'])} — PICKEMUP report</title>
<style>body{{font-family:system-ui;margin:2.5rem auto;max-width:900px;color:#1c1c1e;line-height:1.5}}
h1{{font-size:1.4rem}}table{{border-collapse:collapse;width:100%;font-size:.85rem}}
td,th{{border:1px solid #d0d0d4;padding:6px 8px;text-align:left;vertical-align:top}}
.meta{{color:#666;font-size:.85rem}}.disclaimer{{background:#fff8e6;border:1px solid #e8d9a0;
padding:10px 14px;font-size:.85rem;border-radius:8px}}</style></head><body>
<h1>{e(d['title'])}</h1><p class="meta">{e(d['case_id'])} · analyst {e(d['analyst'])} · status {e(d['status'])}
· generated {e(d['generated_at'][:19])} · evidence manifest <code>{e(d['evidence_manifest_sha256'][:32])}…</code></p>
<h3>Scope</h3><p>{e(d['scope']) or '—'}</p>
<h3>Analyst findings</h3><p>{e(d['findings']) or '—'}</p>
<h3>Evidence ({len(d['evidence'])})</h3><table><tr><th>Kind</th><th>ID</th><th>SHA-256 / conf</th>
<th>Source / verdict</th><th>Excerpt</th><th>Origin</th></tr>{''.join(rows) or '<tr><td colspan=6>none</td></tr>'}</table>
<h3>Notes</h3><ul>{notes or '<li>none</li>'}</ul>
<p class="disclaimer">{e(d['disclaimer'])}</p></body></html>"""


# ---------- alerts ----------

@app.get("/api/v1/alerts")
def list_alerts(status: str | None = "new", s: Session = Depends(session_dep)):
    qs = select(Alert).order_by(Alert.id.desc()).limit(100)
    if status:
        qs = select(Alert).where(Alert.status == status).order_by(Alert.id.desc()).limit(100)
    rows = s.scalars(qs).all()
    # Human-readable subjects: operators must see WHO did WHAT, never a bare id.
    subj_ids = {a.subject_id for a in rows}
    posts = {p.id: p for p in s.scalars(select(Post).where(Post.id.in_(subj_ids))).all()} if subj_ids else {}
    author_ids = {p.author_id for p in posts.values()}
    author_ids |= {a.subject_id for a in rows if a.kind == "automation"}
    accts = {x.id: x for x in s.scalars(select(Account).where(Account.id.in_(author_ids))).all()} if author_ids else {}
    out = []
    for a in rows:
        post = posts.get(a.subject_id)
        acct = accts.get(post.author_id if post else (a.subject_id if a.kind == "automation" else None))
        out.append({"id": a.id, "kind": a.kind, "severity": a.severity, "subject_id": a.subject_id,
                    "evidence": a.evidence, "status": a.status, "created_at": a.created_at.isoformat(),
                    "subject_label": (acct.username if acct else None) or a.subject_id,
                    "account_id": (post.author_id if post else (a.subject_id if a.kind == "automation" else None)),
                    "excerpt": (post.text[:220] if post else None),
                    "post_url": (getattr(post, "source_url", None) if post else None)})
    return out


class AlertIn(BaseModel):
    status: str


@app.post("/api/v1/alerts/{alert_id}")
def update_alert(alert_id: int, body: AlertIn, s: Session = Depends(session_dep)):
    a = s.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")
    if body.status not in ("acked", "false_positive", "suppressed", "new"):
        raise HTTPException(422, "bad status")
    a.status = body.status
    s.add(AuditLog(actor="analyst", action=f"alert.{body.status}", subject=f"alert:{a.id}", detail={}))
    s.commit()
    return {"ok": True}
