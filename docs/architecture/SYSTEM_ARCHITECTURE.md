# PICKEMUP System Architecture (v0.1, implemented)

## Overview

Modular monorepo, local-first. No cloud services, no Redis, no Docker required
for demo/dev mode. Postgres/Redis paths exist behind the same interfaces for
later.

```
Data sources (X API / CSV / JSONL / synthetic)      [packages/connectors]
        │  Connector interface + BudgetGuard (per-job/daily USD caps)
        ▼
Durable job queue (jobs table, atomic claim, retry+backoff, dead-letter)
        │                                                [services/api/jobs.py]
        ▼
Normalization + provenance + evidence hashes → SQLite / PostgreSQL
        │                                                [services/api/pipeline.py, models.py]
        ▼
Intelligence engines (pure-Python, explainable)      [packages/analysis-core]
   automation.py     multi-family signal composite w/ gating
   coordination.py   e1/e2/e3 edge rules, popular-artifact exclusion, Louvain
   propagation.py    near-dup narrative groups + structural repost chains
   graph.py          typed edges (observed vs inferred), centrality, snapshots
        ▼
FastAPI (OpenAPI auto-docs at /docs)                 [services/api/main.py]
        ▼
Next.js analyst dashboard + Cytoscape network explorer [apps/web]
```

## Canonical data model (implemented subset of directive §9)

`Account`, `Post` (hashtags/urls/mentions as JSON, repost/quote/reply refs,
`content_hash` SHA-256, embedded `provenance` incl. file SHA-256 & job id),
`DetectionResult` (kind=automation|edge|cluster|narrative; payload carries
signals, evidence post ids, caveats, `algo_version`), `Job`, `Case`,
`CaseMember`, `CaseNote`, `Alert` (unique `dedupe_key`), `AuditLog`
(append-only), `KV` (worker heartbeat, daily spend, collection-start).

`origin_mode ∈ {live, fixture, mock, synthetic}` is stamped on every record
end-to-end; the UI is required to label anything non-live.

## Engines (measure quality with scripts/evaluate_demo.py)

- **Automation**: signals grouped into families (temporal / content /
  behavioral / metadata). Verdict `likely_automated` requires **≥2 independent
  families with strong signals** — volume alone can never flag a human.
  Sparse data ⇒ explicit `insufficient_evidence`.
- **Coordination**: pairwise signals (exact-URL sharing, non-popular hashtag
  sharing, near-duplicate content, duplicate-anchored temporal sync, synced
  reposts). Edge rules e1/e2/e3; corpus-popular artifacts excluded; domain
  coincidence recorded but never edges alone. Clusters via Louvain; reports
  include organic alternative explanations and downgrade verdict when evidence
  is weak-but-organic-plausible.
- **Propagation**: narrative groups = similar-content union structural repost/
  quote edges; first **observed** post, earlier near-duplicate variants,
  cascade order, amplifiers, coverage gaps, "not true origin" caveat.
- **Graph export**: nodes carry cluster/degree/pagerank/betweenness; edges
  carry kinds + evidence post ids + observed/inferred flag (legend in UI).

## Trust & safety properties (implemented)

- Every detection persists algo_version + evidence ids → re-runnable, auditable.
- Reports carry an evidence manifest (SHA-256 of included records) + standing
  disclaimer distinguishing observation / inference / proof.
- Alerts dedupe by key (no alert storms); false-positive marking supported.
- BudgetGuard refuses collection beyond caps **before** the HTTP call.
- X live adapter raises honestly when credentials absent; mock connector is
  loud about mock mode. No scraping anywhere.
- Audit trail records job lifecycle, case actions, exports.

## Deferred / future (documented, not faked)

- AuthN/RBAC (local MVP is single-user; `X-API-Key` gate available via env),
  Postgres+pgvector, Redis/Celery swap, Leiden upgrade, embedding models and
  Arabic risk classifier (Phase 5), AI investigation assistant, Docker Compose
  file provided but daemon-dependent paths untested here (BLOCKERS B1).
