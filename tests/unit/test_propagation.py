from datetime import datetime, timedelta, timezone

from pickemup_analysis.domain import PostObs
from pickemup_analysis.propagation import group_narrative
from pickemup_analysis.similarity import minhash, minhash_jaccard, shingles, text_similarity

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def test_similarity_basics():
    a = "the quick brown fox jumps over the lazy dog"
    assert text_similarity(a, a) == 1.0
    assert text_similarity(a, "completely different words about cats and rain") < 0.1
    m1, m2 = minhash(a), minhash(a)
    assert minhash_jaccard(m1, m2) == 1.0
    assert "one" in shingles("one")


def test_arabic_text_similarity():
    a = "الوزير افتتح مستشفى الأطفال الجديد اليوم"
    b = "الوزير افتتح مستشفى الأطفال الجديد اليوم بحضور المحافظ"
    assert text_similarity(a, b) > 0.4


def test_narrative_orders_by_time_and_discloses_gaps():
    early = PostObs(post_id="p1", author_id="a1", text="narrative core message alpha",
                    created_at=T0)
    late = PostObs(post_id="p2", author_id="a2", text="narrative core message alpha",
                   created_at=T0 + timedelta(hours=5), repost_of="p1")
    unrelated = PostObs(post_id="p3", author_id="a3", text="totally other subject here",
                        created_at=T0 + timedelta(hours=1))
    groups = group_narrative([late, early], corpus=[early, late, unrelated])
    assert len(groups) == 1
    g = groups[0]
    assert g.first_observed.post_id == "p1"          # earliest OBSERVED
    assert {p.post_id for p in g.posts} == {"p1", "p2"}  # repost joined via structural edge
    assert any("original authorship" in cv for cv in g.caveats)
    assert g.coverage_gaps
