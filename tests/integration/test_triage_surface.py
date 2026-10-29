"""Triage surface: alerts name the account + show the post; risk feed carries
handles; sparse-data automation gives an honest weak-signal verdict."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from pickemup_api.db import SessionLocal
from pickemup_api.main import app
from pickemup_api.models import Account, Alert, Post
from pickemup_analysis.automation import score_account


def client():
    return TestClient(app)


def _seed_post_alert():
    with SessionLocal() as s:
        s.add(Account(id="did:plc:abc123", username="spamm3r9000.bsky.social",
                      origin_mode="live"))
        s.add(Post(id="at://did:plc:abc123/app.bsky.feed.post/rkey1",
                   author_id="did:plc:abc123",
                   text="filth of the system, buy now at spam.example",
                   content_hash="h-t1", origin_mode="live",
                   source_url="https://bsky.app/profile/x/post/rkey1"))
        s.add(Alert(kind="content_risk", severity="high", dedupe_key="t1",
                    subject_id="at://did:plc:abc123/app.bsky.feed.post/rkey1",
                    evidence={"category": "hate_speech", "matched_terms": ["filthy"]}))
        s.commit()


def test_alerts_surface_names_account_and_shows_excerpt():
    _seed_post_alert()
    with client() as c:
        r = c.get("/api/v1/alerts?status=new")
        assert r.status_code == 200
        a = r.json()[0]
        assert a["subject_label"] == "spamm3r9000.bsky.social"
        assert a["account_id"] == "did:plc:abc123"
        assert "filth of the system" in a["excerpt"]
        assert a["post_url"] == "https://bsky.app/profile/x/post/rkey1"


def test_alerts_survive_missing_post_subject():
    with SessionLocal() as s:
        s.add(Alert(kind="cluster", severity="medium", dedupe_key="t2",
                    subject_id="cluster:ghost", evidence={}))
        s.commit()
    with client() as c:
        a = c.get("/api/v1/alerts?status=new").json()[0]
        assert a["subject_label"] == "cluster:ghost"  # honest fallback, no crash
        assert a["account_id"] is None


def test_risk_feed_resolves_author_handle():
    from pickemup_api.models import DetectionResult
    with SessionLocal() as s:
        s.add(Account(id="did:plc:rr7", username="riskauthor.bsky.social", origin_mode="live"))
        s.add(DetectionResult(kind="risk", subject_id="post-r1", score=64.0,
                              verdict="hate_speech", confidence=0.7, algo_version="t",
                              payload={"severity": "high", "language": "en",
                                       "author_id": "did:plc:rr7", "excerpt": "bad words",
                                       "matched_terms": ["x"], "caveats": []}))
        s.commit()
    with client() as c:
        item = c.get("/api/v1/analysis/risk").json()["items"][0]
        assert item["author_handle"] == "riskauthor.bsky.social"


def test_sparse_account_with_weak_profile_gets_honest_indeterminate():
    now = datetime.now(timezone.utc)
    posts = [  # single post -> below MIN_POSTS
        type("P", (), {"post_id": f"p{i}", "author_id": "a1", "text": "hi",
                       "created_at": now - timedelta(minutes=i),
                       "repost_of": None, "reply_to": None, "hashtags": [],
                       "urls": [], "mentions": []})() for i in range(1)]
    weak = score_account("a1", posts, {
        "username": "user8374392.bsky.social", "display_name": "",
        "followers": 0, "following": 0, "default_profile": True,
        "created_at": (now - timedelta(days=3)).isoformat()})
    assert weak.verdict == "indeterminate"
    assert weak.automation_score > 0 and weak.confidence <= 0.25
    assert any("NOT automation evidence" in c for c in weak.caveats)
    assert any("numeric run" in w for w in weak.weak_profile)
    assert any("days old" in w for w in weak.weak_profile)

    fine = score_account("a2", posts, {
        "username": "jane.rivers.bsky.social", "display_name": "Jane Rivers",
        "followers": 412, "following": 288, "default_profile": False,
        "created_at": (now - timedelta(days=900)).isoformat()})
    assert fine.verdict == "insufficient_evidence"
    assert fine.weak_profile == []
