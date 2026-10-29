"""Search, transforms and correlations — the typed-pivot surface.
Covers: entity indexing from the demo corpus, unified search grouping,
transform discovery + execution with lineage, and rule-driven correlation."""
import time

import pytest
from fastapi.testclient import TestClient

from pickemup_api.db import SessionLocal
from pickemup_api.main import app
from pickemup_api.models import Alert, DetectionResult, Job
from pickemup_analysis.entities import extract_entities


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _drain(max_loops: int = 600) -> None:
    from pickemup_api import jobs as jobs_mod
    for _ in range(max_loops):
        with SessionLocal() as s:
            if not s.query(Job).filter(Job.status == "queued").count():
                return
        job = jobs_mod._claim_one("test-worker-piv")
        if job is None:
            time.sleep(0.05)
            continue
        jobs_mod._run_job(job)


def _load_demo(client) -> None:
    r = client.post("/api/v1/demo/load", json={"seed": 77})
    assert r.status_code == 200
    _drain()


# ---------------- pure extractor ----------------

def test_extract_entities_types_and_normalization():
    t = ("Join #SyntheticRally #مستشفى_الأطفال @Bot.x see "
         "https://www.Example.ORG/fund?x=1 team@org.com +966 50 123 4567")
    e = extract_entities(t)
    assert "syntheticrally" in e["HASHTAG"]
    assert any("مستشفى" in h for h in e["HASHTAG"])
    assert "bot.x" in e["MENTION"]
    assert any(u.startswith("https://www.Example") for u in e["URL"])
    assert "example.org" in e["DOMAIN"]           # lowercased + www-stripped
    assert "team@org.com" in e["EMAIL"]
    assert e["PHONE"]                              # phone captured


# ---------------- indexing + search ----------------

def test_demo_pipeline_indexes_entities(client):
    _load_demo(client)
    st = client.get("/api/v1/search/stats").json()
    assert st["types"]["HASHTAG"]["entities"] > 0
    assert st["types"]["DOMAIN"]["entities"] > 0
    assert st["entities_total"] > 10


def test_unified_search_groups_results(client):
    _load_demo(client)
    r = client.get("/api/v1/search", params={"q": "syntheticrally"})
    assert r.status_code == 200
    d = r.json()
    assert any(e["etype"] == "HASHTAG" and "syntheticrally" in e["value"] for e in d["entities"])
    assert len(d["posts"]) > 0
    assert d["posts"][0]["author"]                  # resolved display name
    assert d["totals"]["matched_entities"] > 0


def test_search_requires_query(client):
    assert client.get("/api/v1/search", params={"q": "  "}).status_code == 422


# ---------------- transforms ----------------

def test_transform_discovery_filters_by_input_type(client):
    _load_demo(client)
    d = client.get("/api/v1/transforms", params={"input": "HASHTAG"}).json()
    names = {t["name"] for t in d["transforms"]}
    assert {"posts_containing", "authors", "co_hashtags"} <= names
    # ACCOUNT-only transform must not appear for HASHTAG inputs
    assert "cluster_memberships" not in names
    allm = client.get("/api/v1/transforms").json()["transforms"]
    assert len(allm) > len(d["transforms"])


def test_hashtag_pivots_return_lineage(client):
    _load_demo(client)
    r = client.post("/api/v1/transform", json={
        "input_type": "HASHTAG", "value": "syntheticrally", "name": "posts_containing"})
    assert r.status_code == 200
    res = r.json()
    assert res["results"] and res["results"][0]["evidence"]
    co = client.post("/api/v1/transform", json={
        "input_type": "HASHTAG", "value": "syntheticrally", "name": "co_domains"}).json()
    assert any("example.org" in x["value"] for x in co["results"])


def test_transform_rejects_wrong_input_type(client):
    _load_demo(client)
    r = client.post("/api/v1/transform", json={
        "input_type": "PHONE", "value": "0", "name": "cluster_memberships"})
    assert r.status_code == 422


def test_account_cluster_membership_transform(client):
    _load_demo(client)
    with SessionLocal() as s:
        row = s.query(DetectionResult).filter(DetectionResult.kind == "cluster").first()
        members = (row.payload or {}).get("members") or []
        assert members
        from pickemup_api.models import Account
        user = s.get(Account, members[0])
    r = client.post("/api/v1/transform", json={
        "input_type": "ACCOUNT", "value": user.username, "name": "cluster_memberships"})
    res = r.json()
    assert res["output_type"] == "CLUSTER" and res["results"], res
    assert res["results"][0]["value"] == row.subject_id


# ---------------- correlations ----------------

def test_correlation_watchlist_plus_link_fires_with_evidence(client):
    _load_demo(client)
    # register a watch term that co-occurs with a link in stored demo posts
    r = client.post("/api/v1/lexicon", json={"term": "rally", "severity": "watch",
                                             "note": "test"})
    assert r.status_code in (200, 201)
    _drain()  # lexicon mutation queues a re-score; index already built by demo load
    hits = client.post("/api/v1/correlations/run").json()
    assert hits["rules_evaluated"] == 2
    fired = [h for h in hits["hits"] if h["rule"] == "watchlist_plus_link"]
    assert fired and fired[0]["evidence"]["post_id"]
    with SessionLocal() as s:
        a = s.query(Alert).filter(Alert.kind == "correlation.watchlist_plus_link").first()
        assert a is not None and a.evidence["term"] == "rally"
    # idempotent same-day: re-run raises nothing new
    again = client.post("/api/v1/correlations/run").json()
    assert not [h for h in again["hits"] if h["rule"] == "watchlist_plus_link"]


def test_correlations_list_endpoint(client):
    _load_demo(client)
    d = client.get("/api/v1/correlations").json()
    assert {r["id"] for r in d["rules"]} == {"watchlist_plus_link", "domain_across_clusters"}
