"""Connector-name job shortcut, auto analysis chain after collect,
and human-readable X error on 402 (no credits)."""
import time

import pytest
from fastapi.testclient import TestClient

from pickemup_api.main import app
from pickemup_api import jobs as jobs_mod
from pickemup_api.db import SessionLocal
from pickemup_api.models import Job


@pytest.fixture(autouse=True)
def no_real_env_token(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)


def run_jobs_inline(limit=300):
    """env=test skips the background worker — claim+run jobs in-process."""
    for _ in range(limit):
        with SessionLocal() as s:
            pending = s.query(Job).filter(Job.status.in_(["queued", "retrying"])).count()
        if not pending:
            return
        job = jobs_mod._claim_one("test-worker")
        if job is None:
            time.sleep(0.05)
            continue
        jobs_mod._run_job(job)


def test_connector_name_shortcut_maps_to_collect_and_chains_analysis():
    with TestClient(app) as c:
        r = c.post("/api/v1/jobs", json={"kind": "synthetic_campaign", "params": {"seed": 11}})
        assert r.status_code == 201
        jid = r.json()["id"]
        assert r.json()["kind"] == "collect"  # normalized by the API

        run_jobs_inline()

        j = c.get(f"/api/v1/jobs/{jid}").json()
        assert j["status"] == "done", j["error"]
        assert j["result"]["posts_new"] > 0

        jobs = c.get("/api/v1/jobs", params={"limit": 50}).json()
        kinds = {jj["kind"] for jj in jobs}
        assert {"analyze_content_risk", "analyze_coordination",
                "analyze_automation", "analyze_narratives"} <= kinds, kinds
        assert all(jj["status"] == "done" for jj in jobs), \
            {jj["kind"]: jj["error"] for jj in jobs if jj["status"] != "done"}
        ov = c.get("/api/v1/overview").json()
        assert sum(ov["posts_by_origin"].values()) > 0


def test_collect_error_text_is_human_readable_on_402(monkeypatch):
    import httpx

    def fake_get(*a, **k):
        return httpx.Response(402, json={"title": "Payment Required",
                                         "detail": "Credits is depleted."},
                              request=httpx.Request("GET", "https://api.x.com/2/x"))
    monkeypatch.setattr(httpx, "get", fake_get)  # connector imports httpx in run()

    import pickemup_connectors.x_api as xa
    conn = xa.XApiConnector(token="A" * 40)
    with pytest.raises(RuntimeError) as ei:
        conn.run({"query": "test"}, budget=None)
    msg = str(ei.value)
    assert "402" in msg and "NO CREDITS" in msg and "Billing" in msg, msg
    assert "Credits is depleted." in msg  # X's own detail is preserved


def test_402_job_marks_failed_not_silently_done(monkeypatch):
    import httpx

    def fake_get(*a, **k):
        return httpx.Response(402, json={"title": "Payment Required"},
                              request=httpx.Request("GET", "https://api.x.com/2/x"))
    monkeypatch.setattr(httpx, "get", fake_get)
    with TestClient(app) as c:
        c.put("/api/v1/x-connection", json={"token": "A" * 40})
        r = c.post("/api/v1/jobs", json={"kind": "x_api", "params": {"query": "test"}})
        jid = r.json()["id"]
        job = jobs_mod._claim_one("test-worker")
        if job is not None:
            jobs_mod._run_job(job)
        j = c.get(f"/api/v1/jobs/{jid}").json()
        assert j["status"] != "done"
        assert "402" in (j["error"] or ""), j


def test_production_mode_hides_dev_sources_and_rejects_them(monkeypatch):
    from pickemup_api.config import settings
    monkeypatch.setattr(settings, "pickemup_env", "dev")
    monkeypatch.setattr(settings, "pickemup_dev_tools", False)
    with TestClient(app) as c:
        r = c.get("/api/v1/sources").json()
        assert r["dev_tools"] is False
        # production surface: both OPERATIONAL connectors remain, dev/test ones vanish
        assert set(r["connectors"]) == {"x_api", "bluesky"}
        assert c.post("/api/v1/demo/load", json={"seed": 1}).status_code == 404
        assert c.post("/api/v1/jobs", json={"kind": "synthetic_campaign",
                                            "params": {"seed": 1}}).status_code == 404
        assert c.post("/api/v1/jobs", json={"kind": "collect",
                                            "params": {"connector": "x_api_mock"}}).status_code == 404
        # the real connector is still accepted
        assert c.post("/api/v1/jobs", json={"kind": "x_api",
                                            "params": {"query": "q"}}).status_code == 201
