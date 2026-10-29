import pytest
from pickemup_analysis.domain import PostObs
from pickemup_connectors import BudgetExceeded, BudgetGuard
from pickemup_connectors.file_imports import FileImportConnector
from pickemup_connectors.synthetic import SyntheticCampaignConnector
from pickemup_connectors.x_api import MockXConnector, XApiConnector


def test_budget_guard_blocks_per_job():
    b = BudgetGuard(per_job_usd=1.0, per_day_usd=100, post_read_usd=0.005, user_read_usd=0.010)
    est = b.estimate(post_reads=300, user_reads=0)   # $1.50 > $1 job cap
    assert est.capped_by == "job_budget"
    with pytest.raises(BudgetExceeded):
        b.charge(est)
    ok = b.estimate(post_reads=100, user_reads=0)    # $0.50 fits
    b.charge(ok)
    assert b.job_spent == pytest.approx(0.5)


def test_budget_guard_blocks_daily():
    b = BudgetGuard(per_job_usd=100, per_day_usd=1.0, post_read_usd=0.005, user_read_usd=0.010)
    b.charge(b.estimate(post_reads=120))             # $0.60
    est = b.estimate(post_reads=100)                 # +$0.50 > $1 day
    assert est.capped_by == "daily_budget"
    with pytest.raises(BudgetExceeded):
        b.charge(est)


def test_x_api_honest_about_missing_credentials(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    ok, why = XApiConnector().available()
    assert not ok and "X_BEARER_TOKEN" in why
    with pytest.raises(RuntimeError):
        XApiConnector().run({"query": "x"})


def test_mock_x_connector_announces_mock_mode():
    c = MockXConnector()
    ok, why = c.available()
    assert ok and "MOCK" in why.upper()
    batch = c.run({"query": "test"})
    assert batch.origin_mode == "mock"
    assert batch.provenance["source_kind"] == "x_api_mock"


def test_synthetic_corpus_shape_and_labels():
    batch = SyntheticCampaignConnector().run({"seed": 5})
    assert batch.origin_mode == "synthetic"
    labels = set(batch.provenance["account_ground_truth"].values())
    assert {"automated_coordinated", "human_coordinated", "organic", "benign_human_active"} <= labels
    assert any(p.origin_mode == "synthetic" for p in batch.posts)
    # reproducibility
    b2 = SyntheticCampaignConnector().run({"seed": 5})
    assert [p.post_id for p in b2.posts] == [p.post_id for p in batch.posts]


def test_csv_import_dedup_and_provenance(tmp_path):
    f = tmp_path / "posts.csv"
    f.write_text(
        "post_id,username,account_id,text,created_at,hashtags,urls\n"
        'p1,alice,acct-alice,"hello world #tag1",2026-10-20T10:00:00Z,tag1,https://ex.org/a\n'
        'p1,alice,acct-alice,"hello world #tag1",2026-10-20T10:00:00Z,tag1,https://ex.org/a\n'
        "p2,bob,acct-bob,second post,2026-10-20T10:05:00Z,,\n", encoding="utf-8")
    batch = FileImportConnector("csv").run({"path": str(f)})
    assert len(batch.posts) == 2
    assert len(batch.accounts) == 2
    assert batch.provenance["file_sha256"]
    p = [x for x in batch.posts if x.post_id == "p1"][0]
    assert p.hashtags == ["tag1"] and p.urls == ["https://ex.org/a"]
    assert p.created_at is not None and p.created_at.tzinfo is not None
