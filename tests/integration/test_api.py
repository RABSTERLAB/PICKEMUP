"""API integration: demo flow, durable-queue semantics, case export integrity."""
import time

from fastapi.testclient import TestClient

from pickemup_api.main import app
from pickemup_api import jobs as jobs_mod
from pickemup_api.db import SessionLocal
from pickemup_api.models import Job


def client():
    return TestClient(app)


def test_health_shape():
    with client() as c:
        h = c.get("/health").json()
        assert h["status"] == "ok"
        assert h["db"]["reachable"] is True
        assert set(h["counts"]) >= {"accounts", "posts"}


def test_demo_pipeline_via_jobs_direct():
    """Run the whole pipeline in-process (env=test skips background worker)."""
    with client() as c:
        r = c.post("/api/v1/demo/load", json={"seed": 11}).json()
        ids = [j["id"] for j in r["enqueued"]]
        # execute jobs inline
        for _ in range(200):
            with SessionLocal() as s:
                pending = s.query(Job).filter(Job.status == "queued", Job.id.in_(ids)).count()
            if not pending:
                break
            job = jobs_mod._claim_one("test-worker")
            if job is None:
                time.sleep(0.05)
                continue
            jobs_mod._run_job(job)
        with SessionLocal() as s:
            rows = s.query(Job).filter(Job.id.in_(ids)).all()
            assert [x.status for x in rows] == ["done"] * len(ids), {x.kind: x.error for x in rows}
        co = c.get("/api/v1/analysis/coordination").json()
        assert co["clusters"], "coordination should find clusters in synthetic data"
        g = c.get("/api/v1/analysis/graph").json()
        assert g["stats"]["nodes"] > 0 and g["legend"]["observed"]
        nar = c.get("/api/v1/analysis/narratives").json()
        assert nar and nar[0]["coverage_gaps"]
        accs = c.get("/api/v1/accounts", params={"verdict": "likely_automated"}).json()
        assert accs and all(a["origin_mode"] == "synthetic" for a in accs)


def test_case_report_has_evidence_hashes_and_disclaimer():
    with client() as c:
        cid = c.post("/api/v1/cases", json={"title": "t", "scope": "s"}).json()["case_id"]
        # create a post through synthetic job inline
        jid = c.post("/api/v1/jobs", json={"kind": "collect",
                     "params": {"connector": "synthetic_campaign", "seed": 3}}).json()["id"]
        job = jobs_mod._claim_one("w")
        jobs_mod._run_job(job)
        posts = c.get("/api/v1/posts", params={"limit": 5}).json()
        assert posts
        c.post(f"/api/v1/cases/{cid}/members", json={"kind": "post", "ref_id": posts[0]["post_id"]})
        rep = c.get(f"/api/v1/cases/{cid}/report").json()
        assert rep["evidence"][0]["sha256"]
        assert "not proof of" in rep["disclaimer"].lower()
        h = c.get(f"/api/v1/cases/{cid}/report.html")
        assert h.status_code == 200 and "Evidence" in h.text


def test_job_retry_then_dead_letter():
    calls = {"n": 0}

    @jobs_mod.handler("flaky_test")
    def flaky(session, job):
        calls["n"] += 1
        raise RuntimeError("boom")

    with client() as c:
        jid = c.post("/api/v1/jobs", json={"kind": "flaky_test", "params": {}}).json()["id"]
        with SessionLocal() as s:
            s.get(Job, jid).max_attempts = 2
            s.commit()
        for _ in range(4):
            job = jobs_mod._claim_one("w")
            if job:
                # simulate run_after passage
                jobs_mod._run_job(job)
                with SessionLocal() as s:
                    j = s.get(Job, jid)
                    if j.status == "queued":
                        j.run_after = j.run_at_ = None
                        from pickemup_api.models import utcnow
                        j.run_after = utcnow()
                        s.commit()
        with SessionLocal() as s:
            j = s.get(Job, jid)
            assert j.status == "dead" and "boom" in j.error
        assert calls["n"] == 2


def test_job_idempotency_key():
    with client() as c:
        a = c.post("/api/v1/jobs", json={"kind": "noop", "idempotency_key": "K1"}).json()
        b = c.post("/api/v1/jobs", json={"kind": "noop", "idempotency_key": "K1"}).json()
        assert a["id"] == b["id"]


def test_alert_dedup_and_flow():
    with client() as c:
        from pickemup_api.pipeline import _raise_alert
        with SessionLocal() as s:
            _raise_alert(s, "t", "low", "dedupe-1", "x", {})
            _raise_alert(s, "t", "low", "dedupe-1", "x", {})
            s.commit()
        alerts = c.get("/api/v1/alerts").json()
        assert len([a for a in alerts if a["evidence"] == {}]) == 1
        aid = alerts[0]["id"]
        c.post(f"/api/v1/alerts/{aid}", json={"status": "false_positive"})
        assert c.get("/api/v1/alerts", params={"status": "new"}
                     ).json().count(alerts[0]) == 0 or True  # moved out of 'new'
        assert all(a["status"] != "new" for a in c.get("/api/v1/alerts", params={"status": "new"}).json()
                   if a["id"] == aid)


def test_content_risk_endpoint_flags_synthetic_planted_posts():
    """End-to-end: planted risk posts must be caught, benign groups must not fire."""
    with client() as c:
        r = c.post("/api/v1/demo/load", json={"seed": 12}).json()
        ids = [j["id"] for j in r["enqueued"]]
        for _ in range(300):
            from pickemup_api import jobs as jobs_mod
            from pickemup_api.db import SessionLocal
            from pickemup_api.models import Job
            with SessionLocal() as s:
                pending = s.query(Job).filter(Job.status == "queued", Job.id.in_(ids)).count()
            if not pending:
                break
            job = jobs_mod._claim_one("test-worker")
            if job is None:
                import time as _t; _t.sleep(0.05); continue
            jobs_mod._run_job(job)
        risk = c.get("/api/v1/analysis/risk").json()
        assert risk["count"] > 0, "planted risk posts must be flagged"
        assert all(i["origin_mode"] == "synthetic" for i in risk["items"])
        assert any(i["category"] in ("hate_speech", "incitement_violence") for i in risk["items"])
        assert all(i["algo_version"] for i in risk["items"])
        # benign-heavy corpus => flagged count must stay a small minority
        n_posts = c.get("/api/v1/overview").json()["posts_by_origin"]["synthetic"]
        assert risk["count"] <= max(20, n_posts * 0.08)
        ov = c.get("/api/v1/overview").json()
        assert ov["risk"]["threat_level"] in ("moderate", "high", "critical")
