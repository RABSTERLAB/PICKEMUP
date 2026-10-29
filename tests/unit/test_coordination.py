from datetime import datetime, timedelta, timezone

from pickemup_analysis.coordination import detect_coordination
from pickemup_analysis.domain import AccountObs, PostObs

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _acct(i):
    return AccountObs(account_id=f"a{i}", username=f"user{i}")


def _post(pid, aid, text="", tags=(), urls=(), at=None, repost_of=None):
    return PostObs(post_id=pid, author_id=aid, text=text, created_at=at or T0,
                   hashtags=list(tags), urls=list(urls), repost_of=repost_of)


def coordinated_posts():
    """4 accounts reposting the same 2 seeds within 60s — textbook synced amplification."""
    posts = [_post("s1", "a0", "original seed message one"),
             _post("s2", "a0", "original seed message two")]
    for i in range(1, 5):
        for k, seed in enumerate(("s1", "s2")):
            posts.append(_post(f"r{i}{k}", f"a{i}", "", at=T0 + timedelta(seconds=40 + i * 7),
                               repost_of=seed))
    accts = [_acct(i) for i in range(5)]
    return accts, posts


def test_synced_reposts_produce_cluster_with_evidence():
    accts, posts = coordinated_posts()
    reports, edges, _ = detect_coordination(accts, posts)
    assert reports, "expected at least one cluster"
    c = max(reports, key=lambda r: r.accounts)
    assert c.accounts >= 4
    assert "synced_reposts" in c.signal_mix
    assert any(e.evidence_post_ids for e in edges)
    assert c.caveats and "NOT proven common control" in c.caveats[0]


def test_organic_co_coverage_does_not_cluster():
    """Independent accounts covering one event: same tag, varied times, no repost sync."""
    accts = [_acct(i) for i in range(1, 6)]
    posts = []
    for i in range(1, 6):
        for j in range(3):
            posts.append(_post(f"o{i}{j}", f"a{i}",
                               text=f"Breaking: hospital opened in city {i} today, witnesses say crowd was {j}",
                               tags=["hospital"], urls=[f"https://news{i}.example.org/a{j}"],
                               at=T0 + timedelta(hours=i * 3 + j * 40)))
    reports, edges, _ = detect_coordination(accts, posts)
    assert not any(r.accounts >= 4 and r.verdict == "human_coordinated_possible" for r in reports), \
        f"organic coverage misread as coordination: {[(r.cluster_id, r.accounts, r.verdict) for r in reports]}"


def test_domain_coincidence_alone_never_edges():
    accts = [_acct(i) for i in range(1, 5)]
    posts = []
    for i in range(1, 5):
        for j in range(3):
            posts.append(_post(f"d{i}{j}", f"a{i}", text=f"completely distinct reflection {i}-{j}",
                               urls=[f"https://medium.example.org/page-{i}-{j}"],
                               at=T0 + timedelta(hours=i * 5 + j)))
    reports, edges, _ = detect_coordination(accts, posts)
    assert not edges


def test_near_duplicates_at_random_times_do_not_edge():
    """Same boilerplate text, unsynchronized timing = syndication/breaking news, not coordination."""
    accts = [_acct(i) for i in range(1, 5)]
    posts = []
    for i in range(1, 5):
        for j in range(2):
            posts.append(_post(f"n{i}{j}", f"a{i}",
                               text="Official statement: the ministry will publish results next week",
                               tags=["ministry" if i % 2 else "update"],
                               at=T0 + timedelta(days=i, hours=i * 3 + j * 9)))
    reports, edges, _ = detect_coordination(accts, posts)
    assert not [e for e in edges if e.rule == "e2_synced_duplicate_content"]
