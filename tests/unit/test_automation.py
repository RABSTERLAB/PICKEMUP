from datetime import datetime, timedelta, timezone

from pickemup_analysis.automation import (VERDICT_INSUFFICIENT, VERDICT_LIKELY_AUTOMATED,
                                          VERDICT_LIKELY_HUMAN, score_account)
from pickemup_analysis.domain import PostObs

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _posts(n, gap_s, text_fn=None, jitter=0.0):
    out = []
    for i in range(n):
        ts = T0 + timedelta(seconds=gap_s * i + (i % 3) * jitter)
        out.append(PostObs(post_id=f"p{i}", author_id="a1", text=(text_fn or (lambda _: "unique content number"))(i),
                           created_at=ts, hashtags=["tag"], urls=["https://example.org/x"]))
    return out


def test_clockwork_nerduplicate_is_likely_automated():
    posts = _posts(30, 300, lambda i: f"Join the movement now! share wave {i // 8}")
    r = score_account("a1", posts, {"default_profile": True})
    assert r.verdict == VERDICT_LIKELY_AUTOMATED
    names = {s.name for s in r.signals}
    assert "clock_like_cadence" in names and "near_duplicate_content" in names
    assert 0 < r.confidence <= 1
    assert r.evidence_post_ids


def test_sparse_data_returns_insufficient_not_a_guess():
    r = score_account("a1", _posts(3, 600))
    assert r.verdict == VERDICT_INSUFFICIENT
    assert r.caveats


def test_high_volume_random_human_not_flagged():
    import random
    rnd = random.Random(9)
    posts = []
    for i in range(60):
        ts = T0 + timedelta(seconds=rnd.uniform(0, 48 * 3600))
        posts.append(PostObs(post_id=f"p{i}", author_id="a1",
                             text=f"totally distinct thought {i} about thing-{i * 7}",
                             created_at=ts, urls=[f"https://example.net/{i}"]))
    r = score_account("a1", posts)
    assert r.verdict != VERDICT_LIKELY_AUTOMATED, "volume alone must never flag automation"
    assert any("volume" in cv or "family" in cv for cv in r.caveats) or r.verdict == VERDICT_LIKELY_HUMAN


def test_plain_human_activity_is_likely_human():
    posts = [PostObs(post_id=f"p{i}", author_id="a1",
                     text=f"thought number {i} is about topic {i + 100}",
                     created_at=T0 + timedelta(hours=i * 5, minutes=i),
                     hashtags=[f"day{i}"], urls=[f"https://example.org/{i}"])
             for i in range(8)]
    r = score_account("a1", posts)
    assert r.verdict in (VERDICT_LIKELY_HUMAN, VERDICT_INSUFFICIENT)
