"""AI layer: settings, reports (template + LLM + fallback), delivery, investigator, monitor."""
import json

import pytest
from fastapi.testclient import TestClient

import pickemup_api.ai_routes as ai_routes
import pickemup_api.reporting as reporting
from pickemup_api.db import SessionLocal
from pickemup_api.llm import LLMConfig, LLMError, validate_base_url
from pickemup_api.main import app
from pickemup_api.models import KV, Report, ReportDelivery
from pickemup_api.monitor import diff_state, job_monitor_tick


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def seeded(client):
    r = client.post("/api/v1/demo/load", json={"seed": 1337})
    assert r.status_code == 200
    ids = [j["id"] for j in r.json()["enqueued"]]
    import time
    from pickemup_api import jobs as jobs_mod
    from pickemup_api.models import Job
    for _ in range(400):
        with SessionLocal() as s:
            pending = s.query(Job).filter(Job.status == "queued", Job.id.in_(ids)).count()
        if not pending:
            break
        job = jobs_mod._claim_one("test-worker-ai")
        if job is None:
            time.sleep(0.05)
            continue
        jobs_mod._run_job(job)
    return client


# ---------------------------------------------------------------- base

def test_ai_endpoints_registered(client):
    for url in ("/api/v1/ai/status", "/api/v1/ai/config", "/api/v1/ai/smtp",
                "/api/v1/ai/monitor", "/api/v1/ai/reports"):
        assert client.get(url).status_code == 200, url


def test_llm_status_honest_without_config(client):
    d = client.get("/api/v1/ai/status").json()
    assert d["usable"] is False and "template" in d["fallback"]


def test_base_url_validation_blocks_insecure_and_junk():
    assert validate_base_url("http://evil.example.com/v1") is not None
    assert validate_base_url("https://api.openai.com/v1") is None
    assert validate_base_url("http://localhost:11434/v1") is None
    assert validate_base_url("http://192.168.1.10:8000/v1") is None
    assert validate_base_url("not a url") is not None


def test_llm_config_never_echoes_key_and_rejects_insecure_base(client):
    r = client.put("/api/v1/ai/config", json={"enabled": True, "base_url": "https://api.example.com/v1",
                                              "model": "glm-4.7", "api_key": "sk-secret-123"})
    assert r.status_code == 200
    body = r.json()
    assert "sk-secret" not in json.dumps(body)
    assert body["has_api_key"] is True and body["enabled"] is True
    bad = client.put("/api/v1/ai/config", json={"enabled": True, "base_url": "http://evil.com/v1",
                                                "model": "m"})
    assert bad.status_code == 422
    # omitted api_key keeps the stored one
    r2 = client.put("/api/v1/ai/config", json={"enabled": True, "base_url": "https://api.example.com/v1",
                                               "model": "glm-4.7"})
    with SessionLocal() as s:
        assert s.get(KV, "llm_config").value["api_key"] == "sk-secret-123"


# ---------------------------------------------------------------- reports

def test_report_template_generated_without_llm(client):
    r = client.post("/api/v1/ai/reports", json={"scope": "snapshot", "language": "en", "use_llm": True})
    assert r.status_code == 201
    d = r.json()
    assert d["mode"] == "template" and "PICKEMUP Intelligence Report" in d["content_md"]
    assert len(d["facts_sha256"]) == 64


def test_report_scope_validation(client, seeded):
    assert client.post("/api/v1/ai/reports", json={"scope": "account:nope-xyz"}).status_code == 404


def test_report_arabic(client):
    d = client.post("/api/v1/ai/reports", json={"scope": "snapshot", "language": "ar", "use_llm": False}).json()
    assert "تقرير استخباراتي" in d["content_md"]


def test_report_llm_mode_and_error_fallback(client, monkeypatch):
    client.put("/api/v1/ai/config", json={"enabled": True, "base_url": "https://x.example/v1",
                                          "model": "m", "api_key": "k"})
    monkeypatch.setattr(reporting, "narrate",
                        lambda cfg, f, lang: "# Fake narrative\n\n- @synthbot000 reposts (facts_sha cited) "
                                             "with high confidence per the fact-pack contents here.")
    d = client.post("/api/v1/ai/reports", json={"scope": "snapshot", "use_llm": True}).json()
    assert d["mode"] == "llm" and d["content_md"].startswith("# Fake narrative")

    def boom(cfg, f, lang):
        raise LLMError("provider exploded")
    monkeypatch.setattr(reporting, "narrate", boom)
    d2 = client.post("/api/v1/ai/reports", json={"scope": "snapshot", "use_llm": True}).json()
    assert d2["mode"] == "template" and "provider exploded" in d2["llm_error"]
    # report persisted with facts
    with SessionLocal() as s:
        rep = s.get(Report, d2["id"])
        assert rep.facts["corpus"]["posts"] >= 0 and rep.facts_sha256


def test_report_listing_and_fetch_roundtrip(client):
    rid = client.post("/api/v1/ai/reports", json={"scope": "snapshot", "use_llm": False}).json()["id"]
    lst = client.get("/api/v1/ai/reports").json()
    assert any(x["id"] == rid for x in lst)
    got = client.get(f"/api/v1/ai/reports/{rid}").json()
    assert got["facts"] and got["facts_sha256"] and got["content_md"]


# ---------------------------------------------------------------- delivery

def test_send_requires_smtp(client):
    rid = client.post("/api/v1/ai/reports", json={"scope": "snapshot", "use_llm": False}).json()["id"]
    r = client.post(f"/api/v1/ai/reports/{rid}/send", json={"recipients": ["a@b.gov"], "kind": "authorities"})
    assert r.status_code == 409 and "SMTP" in r.json()["detail"]


def test_send_validates_emails(client):
    rid = client.post("/api/v1/ai/reports", json={"scope": "snapshot", "use_llm": False}).json()["id"]
    assert client.post(f"/api/v1/ai/reports/{rid}/send", json={"recipients": ["nope"]}).status_code == 422
    assert client.post(f"/api/v1/ai/reports/{rid}/send", json={"recipients": []}).status_code == 422


class _FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


def test_send_records_delivery_and_audit(client, monkeypatch):
    import smtplib
    _FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    client.put("/api/v1/ai/smtp", json={"host": "smtp.example.com", "port": 587,
                                        "from_address": "console@example.com",
                                        "username": "u", "password": "p"})
    rid = client.post("/api/v1/ai/reports", json={"scope": "snapshot", "use_llm": False}).json()["id"]
    r = client.post(f"/api/v1/ai/reports/{rid}/send",
                    json={"recipients": ["Authoritiy@MoIExample.gov"], "kind": "authorities"})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert len(_FakeSMTP.sent) == 1
    msg = _FakeSMTP.sent[0]
    assert "authorities" in msg["Subject"].lower()
    assert msg["To"] == "authoritiy@moiexample.gov"
    parts = [p.get_content() for p in msg.walk() if p.get_content_type() == "text/plain"]
    assert len(parts) == 1
    body = parts[0]
    assert "analytical inference" in body          # disclaimer travels with the email
    assert "PICKEMUP Intelligence Report" in body  # real content, not a stub
    assert "not proof of identity" in body
    html_parts = [p.get_content() for p in msg.walk() if p.get_content_type() == "text/html"]
    assert html_parts and "<h1>" in html_parts[0]
    with SessionLocal() as s:
        d = s.query(ReportDelivery).filter_by(report_id=rid).one()
        assert d.recipients == ["authoritiy@moiexample.gov"] and d.recipient_kind == "authorities"
        assert d.status == "sent"


def test_send_failure_is_recorded_not_raised(client, monkeypatch):
    import smtplib

    class Bad(_FakeSMTP):
        def send_message(self, msg):
            raise OSError("connection reset")
    monkeypatch.setattr(smtplib, "SMTP", Bad)
    client.put("/api/v1/ai/smtp", json={"host": "smtp.example.com", "from_address": "c@example.com"})
    rid = client.post("/api/v1/ai/reports", json={"scope": "snapshot", "use_llm": False}).json()["id"]
    r = client.post(f"/api/v1/ai/reports/{rid}/send", json={"recipients": ["user@corp.sa"], "kind": "user"})
    assert r.status_code == 200 and r.json()["status"] == "failed"
    with SessionLocal() as s:
        assert s.query(ReportDelivery).filter_by(report_id=rid).one().status == "failed"


# ---------------------------------------------------------------- investigator

class FakeLLM:
    """Scripted tool-calling sequence, then final answer."""

    def __init__(self, cfg, steps=None, final="# Investigation\n\nDone. Cites @handle per tools."):
        self.steps = steps or [{"tool": "corpus_stats", "args": {}},
                               {"tool": "list_clusters", "args": {}}]
        self.final = final
        self.i = 0

    def chat(self, messages, tools=None, tool_choice="auto"):
        if self.i < len(self.steps):
            s = self.steps[self.i]
            self.i += 1
            return {"kind": "tool_call", "name": s["tool"], "arguments": s["args"],
                    "raw_message": {"tool_calls": [{"id": f"call{self.i}", "function":
                                                    {"name": s["tool"], "arguments": "{}"}}]}}
        return {"kind": "answer", "content": self.final}


def test_investigator_requires_llm(client):
    r = client.post("/api/v1/ai/investigate", json={"focus": "x"})
    assert r.status_code == 409


def test_investigator_tool_loop(client, seeded, monkeypatch):
    client.put("/api/v1/ai/config", json={"enabled": True, "base_url": "https://x.example/v1",
                                          "model": "m", "api_key": "k"})
    monkeypatch.setattr(reporting, "LLMClient", FakeLLM)
    r = client.post("/api/v1/ai/investigate", json={"focus": "amplification"})
    assert r.status_code == 201
    d = r.json()
    assert d["mode"] == "llm" and d["content_md"].startswith("# Investigation")
    tools = [s["tool"] for s in d["facts"]["tool_steps"]]
    assert tools == ["corpus_stats", "list_clusters"]


def test_investigator_feeds_tool_errors_back(client, seeded, monkeypatch):
    client.put("/api/v1/ai/config", json={"enabled": True, "base_url": "https://x.example/v1",
                                          "model": "m", "api_key": "k"})
    monkeypatch.setattr(reporting, "LLMClient",
                        lambda cfg: FakeLLM(cfg, steps=[{"tool": "no_such_tool", "args": {}}]))
    r = client.post("/api/v1/ai/investigate", json={"focus": "z"})
    assert r.status_code == 201
    assert r.json()["facts"]["tool_steps"][0]["error"] is True


# ---------------------------------------------------------------- monitor

def test_diff_state_detects_escalation_new_cluster_burst():
    prev = {"verdicts": {"a1": "likely_human"}, "clusters": {"c1": 1}}
    cur = {"verdicts": {"a1": "likely_automated", "a2": "mixed_automation"},
           "clusters": {"c1": 1, "c2": 1}, "new_posts": 200, "burst_threshold": 150}
    hits = diff_state(prev, cur)
    kinds = {h["kind"] for h in hits}
    assert kinds == {"monitor.automation_escalation", "monitor.new_cluster", "monitor.collection_burst"}
    assert any(h["severity"] == "high" and h["subject"] == "a1" for h in hits)


def test_monitor_tick_reports_and_emails_user_never_authorities_unless_enabled(seeded, monkeypatch):
    import smtplib
    _FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    with SessionLocal() as s:
        from types import SimpleNamespace
        s.add(KV(key="monitor_config", value={"enabled": True, "auto_report": True,
                                              "email_user": True, "email_authorities": True,
                                              "user_emails": ["analyst@corp.sa"],
                                              "authorities_emails": ["police@moi.gov"],
                                              "language": "en", "burst_post_threshold": 99999}))
        s.add(KV(key="smtp_config", value={"host": "smtp.example.com", "from": "c@example.com",
                                           "port": 587, "tls": True}))
        s.commit()
        out = job_monitor_tick(s, SimpleNamespace(id=0))
        assert out["report"], "first tick must auto-generate a report"
        assert out["new_posts_since_last"] >= 0
    # user always emailed; authorities only when actionable alerts exist
    subjects = [m["To"] for m in _FakeSMTP.sent]
    assert any("analyst@corp.sa" in t for t in subjects)
    if out["findings"] > 0:
        assert any("police@moi.gov" in t for t in subjects)
