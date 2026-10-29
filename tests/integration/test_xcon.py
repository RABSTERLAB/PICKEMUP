"""X connection management (token never echoed), spend-cap overrides, purge."""
from fastapi.testclient import TestClient

from pickemup_api.main import app

import pytest


@pytest.fixture(autouse=True)
def no_real_env_token(monkeypatch):
    # the operator's .env token must never leak into tests (would hit real X)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)


def client():
    return TestClient(app)


MASKED = "ABCDEF0123456789ABCDEF0123456789"


def test_token_save_keep_clear_and_masking():
    with client() as c:
        r = c.put("/api/v1/x-connection", json={"token": MASKED})
        assert r.status_code == 200
        body = r.json()
        assert body["token_set"] and body["token_source"] == "settings"
        # the raw secret must never appear in ANY response
        assert MASKED not in r.text
        assert "ABCD" in body["token_mask"] and "…" in body["token_mask"]

        # omitting the token keeps it
        body2 = c.put("/api/v1/x-connection", json={"per_job_usd": 1.5}).json()
        assert body2["token_set"] and body2["per_job_usd"] == 1.5

        # empty string clears
        body3 = c.put("/api/v1/x-connection", json={"token": ""}).json()
        assert not body3["token_set"]


def test_token_validation_rejects_junk():
    with client() as c:
        assert c.put("/api/v1/x-connection", json={"token": "short"}).status_code == 422
        assert c.put("/api/v1/x-connection",
                     json={"token": "SPACE IN TOKEN 0123456789ABCDEF"}).status_code == 422


def test_probe_without_token_is_409_no_network():
    with client() as c:
        c.put("/api/v1/x-connection", json={"token": ""})
        r = c.post("/api/v1/x-connection/probe")
        assert r.status_code == 409


def test_caps_drive_job_defaults():
    from sqlalchemy import delete
    from pickemup_api.db import SessionLocal
    from pickemup_api.models import KV
    from pickemup_api.xcon import caps
    with SessionLocal() as s:
        s.execute(delete(KV).where(KV.key == "x_connection"))
        s.commit()
        j, d = caps(s)
        assert j > 0 and d > 0  # env/defaults still resolve


def test_purge_removes_data_keeps_config_and_audit():
    from pickemup_api.db import SessionLocal
    from pickemup_api.models import (Account, Alert, AuditLog, CustomTerm, KV, Post)
    with client() as c:
        # seed config that must survive + data that must not
        c.put("/api/v1/x-connection", json={"token": MASKED, "per_day_usd": 3.0})
        c.post("/api/v1/lexicon", json={"term": "qusos alkhilafa", "severity": "watch"})
        with SessionLocal() as s:
            s.add(Account(id="acct-x1", username="x1", origin_mode="synthetic"))
            s.add(Post(id="post-x1", author_id="acct-x1", text="hello world",
                       content_hash="h-x1",
                       origin_mode="synthetic"))
            s.add(Alert(kind="unit", severity="low", dedupe_key="purge-test",
                        subject_id="post-x1", evidence={}))
            s.commit()
        r = c.post("/api/v1/data/purge", json={"confirm": "NOPE"})
        assert r.status_code == 422  # confirm gate
        r = c.post("/api/v1/data/purge", json={"confirm": "PURGE"})
        assert r.status_code == 200
        purged = r.json()["purged"]
        assert purged["posts"] >= 1 and purged["alerts"] >= 1

        with SessionLocal() as s:
            assert s.query(Post).filter_by(id="post-x1").first() is None
            assert s.query(Account).filter_by(id="acct-x1").first() is None
            assert s.query(Alert).filter_by(dedupe_key="purge-test").first() is None
            # survivors:
            assert s.query(CustomTerm).filter_by(term="qusos alkhilafa").first() is not None
            assert s.get(KV, "x_connection") is not None
            assert s.query(AuditLog).filter_by(action="data.purge").first() is not None
        # token + caps survived the purge
        xc = c.get("/api/v1/x-connection").json()
        assert xc["token_set"] and xc["per_day_usd"] == 3.0


def test_whitespace_token_rejected_not_silently_cleared():
    """OCR audit finding: a whitespace paste must not wipe the stored token."""
    with client() as c:
        c.put("/api/v1/x-connection", json={"token": MASKED})
        r = c.put("/api/v1/x-connection", json={"token": "   \t "})
        assert r.status_code == 422
        assert c.get("/api/v1/x-connection").json()["token_set"] is True
        # explicit empty string remains the intentional clear path
        assert c.put("/api/v1/x-connection", json={"token": ""}).status_code == 200
        assert c.get("/api/v1/x-connection").json()["token_set"] is False


def test_corrupt_persisted_caps_fall_back_instead_of_500():
    """Garbage in the KV blob must degrade to defaults, never 500 the settings read."""
    from pickemup_api.db import SessionLocal
    from pickemup_api.models import KV
    from pickemup_api.xcon import DEFAULT_PER_JOB_USD
    s = SessionLocal()
    row = s.get(KV, "x_connection")
    d = (row.value if row else {}) or {}
    d["per_job_usd"] = "not-a-number"
    if row:
        row.value = d
    else:
        s.add(KV(key="x_connection", value=d))
    s.commit(); s.close()
    with client() as c:
        r = c.get("/api/v1/x-connection")
        assert r.status_code == 200
        assert r.json()["per_job_usd"] == DEFAULT_PER_JOB_USD


def test_mask_hides_more_than_prefix_and_length():
    """Policy: mask exposes prefix + length only - no trailing characters."""
    with client() as c:
        c.put("/api/v1/x-connection", json={"token": MASKED})
        m = c.get("/api/v1/x-connection").json()["token_mask"]
        assert MASKED[-4:] not in m
