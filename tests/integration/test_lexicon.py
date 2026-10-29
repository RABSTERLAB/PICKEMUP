"""Operator-extensible trigger-word watchlist: API, engine injection, re-score, audit."""
import time

import pytest
from fastapi.testclient import TestClient

from pickemup_api.db import SessionLocal
from pickemup_api.main import app
from pickemup_api.models import Alert, AuditLog, DetectionResult, Job, Post
from pickemup_analysis.risk import WATCHLIST_CAT, classify, matrix_scan


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _drain(max_loops: int = 400) -> None:
    from pickemup_api import jobs as jobs_mod
    for _ in range(max_loops):
        with SessionLocal() as s:
            if not s.query(Job).filter(Job.status == "queued").count():
                return
        job = jobs_mod._claim_one("test-worker-lex")
        if job is None:
            time.sleep(0.05)
            continue
        jobs_mod._run_job(job)


# ---------------- pure engine ----------------

def test_custom_terms_match_like_matrix_terms():
    custom = {"qusos alkhilafa": (WATCHLIST_CAT, 0.85)}
    r = classify("support the QUSOS ALKHILAFA attack now".lower(), custom=custom)
    assert r.category == "watchlist_match" and r.action == "ACTION_BLOCK"
    assert "qusos alkhilafa" in r.matched_matrix_terms
    # severity tiers map to action bands
    r_watch = classify("support qusos alkhilafa now", custom={"qusos alkhilafa": (WATCHLIST_CAT, 0.45)})
    assert r_watch.action == "ACTION_FLAG_HUMAN_REVIEW"
    # absent term -> unchanged behaviour
    r_clean = classify("normal text about the weather", custom=custom)
    assert r_clean.category == "benign" and r_clean.action == "ACTION_ALLOW"
    # fuzzy tolerance applies to custom terms too (one edit)
    assert "qusos alkhilafa" in matrix_scan("qusos alkhilaf attack", extra=custom)[3]
    # Arabic-script phrase
    r_ar = classify("انضم لخريجي الجماعه اليوم", custom={"خريجي الجماعه": (WATCHLIST_CAT, 0.85)})
    assert r_ar.category == "watchlist_match"


def test_no_custom_means_zero_regression():
    # the curated matrix behaves exactly as before when no watchlist is passed
    t, p, cats, hits = matrix_scan("some totally benign sentence")
    assert (t, p, cats, hits) == (0.0, 0.0, [], [])


# ---------------- API ----------------

def test_lexicon_crud_validation_and_audit(client):
    r = client.get("/api/v1/lexicon")
    assert r.status_code == 200
    body = r.json()
    assert body["built_in"]["total_terms"] > 40 and body["custom"] == []

    bad = client.post("/api/v1/lexicon", json={"term": "x", "severity": "nonsense"})
    assert bad.status_code == 422

    added = client.post("/api/v1/lexicon", json={"term": "  qusos   alkhilafa ",
                                                 "severity": "severe", "note": "alias"})
    assert added.status_code == 200
    tid = added.json()["id"]
    assert added.json()["term"] == "qusos alkhilafa"  # whitespace collapsed
    assert added.json()["rescore_job"]

    dup = client.post("/api/v1/lexicon", json={"term": "QUSOS ALKHILAFA"})
    assert dup.status_code == 409  # case-insensitive duplicate guard

    off = client.patch(f"/api/v1/lexicon/{tid}", json={"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False

    deleted = client.delete(f"/api/v1/lexicon/{tid}")
    assert deleted.status_code == 200 and deleted.json()["deleted"] == "qusos alkhilafa"

    with SessionLocal() as s:
        actions = [a.action for a in s.query(AuditLog).filter(
            AuditLog.actor == "analyst").all()]
        assert "lexicon.add" in actions and "lexicon.update" in actions and "lexicon.delete" in actions


def test_lexicon_search_covers_builtin_matrix(client):
    from pickemup_analysis.risk import MATRIX
    terms = next(v for v in MATRIX.values() if v)
    # pick a built-in term of length >= 6 to search a fragment of
    term = next(t for t in terms if len(t) >= 6 and " " not in t)
    r = client.get("/api/v1/lexicon/search", params={"q": term[2:5]})
    assert r.status_code == 200
    assert any(m["term"] == term for m in r.json()["built_in"])


def test_watchlist_term_rescores_corpus_end_to_end(client):
    assert client.post("/api/v1/demo/load", json={"seed": 1337}).status_code == 200
    _drain()
    # find a clean, distinctive phrase actually present in the corpus
    with SessionLocal() as s:
        posts = s.query(Post).all()
        target_word = None
        for p in posts:
            words = [w for w in (p.text or "").lower().split() if len(w) >= 7 and w.isalpha()]
            if words and classify(p.text or "").category == "benign":
                target_word = words[0]
                break
    assert target_word, "no suitable benign word found"

    r = client.post("/api/v1/lexicon", json={"term": target_word, "severity": "severe",
                                             "note": "end-to-end test"})
    assert r.status_code == 200
    _drain()

    with SessionLocal() as s:
        hits = s.query(DetectionResult).filter(DetectionResult.kind == "risk").all()
        matched = [h for h in hits if target_word in (h.payload.get("moderation") or {}).get("matched_terms", [])]
        assert matched, f"watchlist term '{target_word}' produced no risk detections after re-score"
        assert all(h.payload["moderation"]["action"] == "ACTION_BLOCK" for h in matched)
        alerts = s.query(Alert).filter(Alert.kind == "content_risk").all()
        assert any(target_word in str(a.evidence) for a in alerts)

        # disabling the term removes detections on the next re-score
        tid = r.json()["id"]
        client.patch(f"/api/v1/lexicon/{tid}", json={"enabled": False})
        _drain()
        s.rollback()  # end this session's snapshot so worker writes are visible
        hits = s.query(DetectionResult).filter(DetectionResult.kind == "risk").all()
        assert not any(target_word in (h.payload.get("moderation") or {}).get("matched_terms", [])
                       for h in hits)
