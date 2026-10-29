"""Regressions for the OpenCodeReview audit (session 7).
One test per class of real defect found by the ocr scan and verified by hand."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pickemup_api.db import SessionLocal
from pickemup_api.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ---------- risk.py ----------

def test_uppercase_watchlist_term_matches():
    from pickemup_analysis.risk import WATCHLIST_CAT, classify
    r = classify("support the QUSOS ALKHILAFA push today",
                 custom={"qusos alkhilafa": (WATCHLIST_CAT, 0.85)})
    assert "qusos alkhilafa" in r.matched_matrix_terms
    assert r.category == "watchlist_match" and r.action == "ACTION_BLOCK"


def test_uppercase_term_key_matches_lowercase_text():
    from pickemup_analysis.risk import WATCHLIST_CAT, classify
    r = classify("support qusos alkhilafa expansion now",
                 custom={"QUSOS ALKHILAFA": (WATCHLIST_CAT, 0.85)})
    assert r.action == "ACTION_BLOCK"  # operator-typed UPPERCASE must not dead-store


def test_flag_action_requires_human_review():
    from pickemup_analysis.risk import WATCHLIST_CAT, classify
    d = classify("support qusos alkhilafa expansion now",
                 custom={"qusos alkhilafa": (WATCHLIST_CAT, 0.65)}).to_dict()
    assert d["moderation"]["action"] == "ACTION_FLAG_HUMAN_REVIEW"
    assert d["human_review_required"] is True


def test_violent_matrix_only_is_not_hate_speech_category():
    from pickemup_analysis.risk import classify
    r = classify("اقتلوا اقتلوا اقتلوا")
    assert r.category == "incitement_violence"


# ---------- entities.py ----------

def test_domain_strips_port_userinfo_fragment():
    from pickemup_analysis.entities import extract_entities
    e = extract_entities("go https://example.com:8443/x and http://u:***@host.co.uk#f "
                         "plus www_plain.com plain")
    assert "example.com" in e["DOMAIN"]
    assert "host.co.uk" in e["DOMAIN"]
    assert not any(":" in d or "@" in d for d in e["DOMAIN"])


# ---------- graph.py ----------

def test_pagerank_conserves_rank_mass():
    import networkx as nx
    from pickemup_analysis.graph import _pagerank_pure
    g = nx.Graph([("a", "b"), ("b", "c")])
    g.add_node("dangling")
    assert abs(sum(_pagerank_pure(g).values()) - 1.0) < 1e-6


def test_temporal_frames_clamps_bad_bucket():
    import networkx as nx
    from pickemup_analysis.graph import GraphSnapshot, temporal_frames
    from pickemup_analysis.domain import PostObs

    def po(i, at):
        return PostObs(post_id=f"p{i}", author_id="a", text="t", created_at=at)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snap = GraphSnapshot(nodes=[], edges=[], clusters={},
                         stats={"nodes": 0, "edges": 0, "clusters": 0, "density": 0.0})
    frames = temporal_frames(snap, [po(0, t0), po(1, t0 + timedelta(hours=5))],
                             bucket_minutes=0)
    assert 1 < len(frames) <= 2001


# ---------- monitor.py ----------

def test_monitor_since_parses_iso_and_state_merges(client):
    from pickemup_api.models import Account, Job, KV, Post, utcnow
    from pickemup_api.monitor import STATE_KV, _current_state, job_monitor_tick
    with SessionLocal() as s:
        if not s.get(Account, "audit-a1"):
            s.add(Account(id="audit-a1", username="audituser1", origin_mode="synthetic"))
            s.add(Post(id="audit-p1", author_id="audit-a1", text="hello world",
                       created_at=utcnow(), collected_at=utcnow(),
                       content_hash="h-audit-1", origin_mode="synthetic"))
            s.commit()
        last = (utcnow() - timedelta(hours=1)).isoformat()
        assert _current_state(s, last)["new_posts"] == 1   # used to raise on raw str
        assert _current_state(s, "garbage")["new_posts"] == 0
        s.merge(KV(key=STATE_KV, value={"last_enqueued": "keep-me", "last_run": last}))
        s.commit()
        fake = type("J", (), {"id": 0})()
        job_monitor_tick(s, fake)
        assert s.get(KV, STATE_KV).value.get("last_enqueued") == "keep-me"


# ---------- reporting.py ----------

def test_cluster_scope_report_does_not_500(client):
    from pickemup_api.reporting import generate_report
    with SessionLocal() as s:
        rep = generate_report(s, "cluster:C99", "en", use_llm=False, created_by="test")
        assert rep.id and rep.facts.get("scope") == "cluster:C99"
        assert rep.content_md and len(rep.content_md) > 500


# ---------- jobs.py ----------

def test_enqueue_duplicate_key_preserves_caller_transaction(client):
    from pickemup_api.jobs import enqueue
    from pickemup_api.models import Account, Post
    with SessionLocal() as s:
        enqueue(s, "analyze_automation", {}, idempotency_key="audit-idem")
        s.add(Account(id="audit-a2", username="audituser2", origin_mode="synthetic"))
        s.add(Post(id="audit-p2", author_id="audit-a2", text="second",
                   created_at=datetime.now(timezone.utc),
                   content_hash="h-audit-2", origin_mode="synthetic"))
        dup = enqueue(s, "analyze_automation", {}, idempotency_key="audit-idem")
        s.commit()  # used to lose the staged Account/Post to a blanket rollback
        assert s.get(Post, "audit-p2") is not None
        assert dup.kind == "analyze_automation"


def test_startup_reclaims_running_jobs(client):
    from pickemup_api.jobs import Worker
    from pickemup_api.models import Job
    with SessionLocal() as s:
        s.add(Job(kind="analyze_automation", params={}, status="running", locked_by="dead-w"))
        s.commit()
    w = Worker(threads=1, poll_seconds=30)
    assert w._reclaim_stale() >= 1
    with SessionLocal() as s:
        stuck = s.query(Job).filter(Job.status == "running", Job.locked_by == "dead-w").count()
        assert stuck == 0
