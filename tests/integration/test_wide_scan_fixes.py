"""Regression tests for the wide-scan audit remediation.

Each test pins one verified defect: correlation NameError, budget cap bypass,
URL-entity hygiene, unlabeled-origin masquerade, propagation chain truncation,
JSONL line poisoning, double-worker job re-queue, CORS/auth middleware order.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import pickemup_api.main as main
from fastapi.testclient import TestClient
from pickemup_api.correlate import RULES
from pickemup_api.db import SessionLocal
from pickemup_api.jobs import Worker
from pickemup_api.models import (AuditLog, DetectionResult, Entity, EntityPost,
                                  Job, KV)
from pickemup_connectors import BudgetExceeded, BudgetGuard
from pickemup_connectors.file_imports import FileImportConnector
from pickemup_analysis.domain import PostObs, domain_of
from pickemup_analysis.propagation import group_narrative


def _obs(pid, author, ts, **kw):
    return PostObs(post_id=pid, author_id=author, text=f"text {pid}",
                   created_at=ts, collected_at=ts, **kw)


@pytest.fixture()
def client():
    return TestClient(main.app)


# --- correlate: domain_spread rule used an undefined `links` var (NameError)
def test_domain_spread_rule_executes_and_carries_post_ids():
    fn = next(r["fn"] for r in RULES if r["id"] == "domain_across_clusters")
    with SessionLocal() as s:
        dom = Entity(etype="DOMAIN", value="badlink.example", post_count=5)
        s.add(dom)
        s.flush()
        for i in range(4):
            s.add(EntityPost(entity_id=dom.id, post_id=f"p{i}", author_id=f"a{i}"))
        s.add(DetectionResult(kind="cluster", subject_id="C1", score=0.5,
                              verdict="suspected_coordinated", confidence=0.5,
                              algo_version="test",
                              payload={"members": ["a1", "a2"]}))
        s.commit()
        out = fn(s)
    assert len(out) == 1
    assert out[0]["evidence"]["post_ids"], "rule must carry real evidence posts"
    assert set(out[0]["evidence"]["post_ids"]) <= {"p0", "p1", "p2", "p3"}


# --- BudgetGuard: negative reads used to EARN budget and dodge both caps
def test_budget_guard_cannot_be_flooding_with_negative_reads():
    g = BudgetGuard(per_job_usd=1.0, per_day_usd=2.0,
                    post_read_usd=0.01, user_read_usd=0.01)
    est = g.estimate(-5000, -5000)
    assert est.usd == 0 and est.capped_by is None
    g.charge(est)
    assert g.job_spent == 0 and g.day_spent == 0
    est.post_reads = 1  # fraction clamps too
    assert g.estimate(0.9).post_reads == 0
    # a hand-forced negative estimate is refused at charge time
    est2 = g.estimate(1)
    est2.usd = -99.0
    with pytest.raises(BudgetExceeded):
        g.charge(est2)


def test_domain_of_strips_credentials_ports_and_junk():
    assert domain_of("https://alice:secretpw@evil.com:8443/path?x=1") == "evil.com"
    assert domain_of("HTTP://Example.COM/a") == "example.com"
    assert domain_of("javascript:alert(1)") == ""
    assert domain_of("not a url") == ""
    assert domain_of("//cdn.site.co.uk/js") == "cdn.site.co.uk"


def test_unlabeled_posts_default_to_fixture_not_live():
    # unlabeled data must never masquerade as collected live evidence
    p = _obs("p1", "a1", datetime.now(timezone.utc))
    assert p.origin_mode == "fixture"
    q = PostObs(post_id="x", author_id="a", text="",
                origin_mode="live")
    assert q.origin_mode == "live"


# --- propagation: unordered corpus used to truncate repost chains
def test_repost_chain_joins_full_depth_regardless_of_order():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    a = _obs("A", "alice", t)
    b = _obs("B", "bob", t + timedelta(minutes=2), repost_of="A")
    c = _obs("C", "cara", t + timedelta(minutes=4), repost_of="B")
    groups = group_narrative([a], corpus=[c, b])  # worst-case order
    assert len(groups) == 1
    assert {p.post_id for p in groups[0].posts} == {"A", "B", "C"}


# --- file_imports: one malformed JSONL line used to abort the whole import
def test_malformed_jsonl_line_skipped_not_fatal(tmp_path):
    f = tmp_path / "feed.jsonl"
    f.write_text(
        '{"post_id":"p1","username":"u1","text":"ok"}\n'
        '{"post_id" broken json\n'
        '{"post_id":"p2","username":"u2","text":"also ok"}\n',
        encoding="utf-8")
    batch = FileImportConnector("jsonl").run({"path": str(f)})
    ids = {p.post_id for p in batch.posts}
    assert ids == {"p1", "p2"}
    assert any("line 2" in e for e in batch.provenance.get("row_errors", []))


# --- worker: startup reclaim must refuse to double-run a LIVE worker's jobs
def test_startup_reclaim_skips_while_another_worker_heartbeats():
    with SessionLocal() as s:
        s.add(Job(kind="collect", params={}, status="running", locked_by="other"))
        s.add(KV(key="worker", value={"worker_id": "other",
                                      "ts": datetime.now(timezone.utc).isoformat()}))
        s.commit()
    assert Worker()._reclaim_stale() == 0
    with SessionLocal() as s:
        assert s.scalar(select(Job).where(Job.kind == "collect")).status == "running"
        assert s.scalars(select(AuditLog).where(AuditLog.action ==
                                                "job.reclaim_skipped")).first()


def test_startup_reclaim_still_recovers_dead_worker_jobs():
    with SessionLocal() as s:
        s.add(Job(kind="collect", params={}, status="running", locked_by="dead"))
        s.add(KV(key="worker", value={"worker_id": "dead",
                                      "ts": (datetime.now(timezone.utc)
                                             - timedelta(minutes=5)).isoformat()}))
        s.commit()
    assert Worker()._reclaim_stale() == 1
    with SessionLocal() as s:
        assert s.scalar(select(Job).where(Job.kind == "collect")).status == "queued"


# --- middleware order: keyed client preflight + 401 responses need CORS headers
def test_cors_wraps_api_key_guard(client):
    origin = "http://localhost:3001"
    key = main.settings.pickemup_api_key
    main.settings.pickemup_api_key = "secret-key"
    try:
        pre = client.options("/api/v1/posts",
                             headers={"Origin": origin,
                                      "Access-Control-Request-Method": "GET"})
        assert pre.status_code in (200, 204)
        assert pre.headers.get("access-control-allow-origin") == origin
        bad = client.get("/api/v1/posts", headers={"Origin": origin,
                                                   "x-api-key": "wrong"})
        assert bad.status_code == 401
        assert bad.headers.get("access-control-allow-origin") == origin
    finally:
        main.settings.pickemup_api_key = key
