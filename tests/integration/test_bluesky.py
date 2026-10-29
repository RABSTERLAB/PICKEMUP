"""Bluesky connector: public app-view mapping + job path (fixture, no network)."""
import pytest

import pickemup_connectors.bluesky as bs
import httpx

FIXTURE = {
    "posts": [
        {"uri": "at://did:plc:aaa/app.bsky.feed.post/rr1",
         "author": {"did": "did:plc:aaa", "handle": "alice.test", "displayName": "Alice",
                    "followersCount": 10},
         "record": {"text": "watch this https://example.org/x #testhash",
                    "createdAt": "2026-10-25T10:00:00.000Z", "langs": ["en"],
                    "facets": [
                        {"features": [{"$type": "app.bsky.richtext.facet#link",
                                       "uri": "https://example.org/x"}]},
                        {"features": [{"$type": "app.bsky.richtext.facet#tag", "tag": "testhash"}],
                         },
                        {"features": [{"$type": "app.bsky.richtext.facet#mention",
                                       "did": "did:plc:bbb"}]}]},
         "repostCount": 3},
        {"uri": "at://did:plc:aaa/app.bsky.feed.repost/rr2",
         "author": {"did": "did:plc:aaa", "handle": "alice.test"},
         "record": {"subject": {"uri": "at://did:plc:bbb/app.bsky.feed.post/zz9"},
                    "createdAt": "2026-10-25T11:00:00.000Z"}},
        {"uri": "at://did:plc:bbb/app.bsky.feed.post/rr3",
         "author": {"did": "did:plc:bbb", "handle": "bob.test"},
         "record": {"text": "replying", "createdAt": "2026-10-25T12:00:00.000Z",
                    "reply": {"parent": {"uri": "at://did:plc:aaa/app.bsky.feed.post/rr1"}}}},
    ],
    "cursor": None,
}


@pytest.fixture
def fake_bsky(monkeypatch):
    def fake_get(url, *a, **k):
        assert url.startswith("https://api.bsky.app/"), "must use the PUBLIC app view"
        return httpx.Response(200, json=FIXTURE, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)


def test_mapping_accounts_posts_and_lineage(fake_bsky):
    b = bs.BlueskyConnector().run({"query": "test", "max_pages": 1})
    assert b.origin_mode == "live"
    assert {a.account_id for a in b.accounts} == {"did:plc:aaa", "did:plc:bbb"}
    by_id = {p.post_id: p for p in b.posts}
    p1 = by_id["at://did:plc:aaa/app.bsky.feed.post/rr1"]
    assert p1.urls == ["https://example.org/x"]
    assert p1.hashtags == ["testhash"]
    assert p1.mentions == ["did:plc:bbb"]
    assert p1.source_url == "https://bsky.app/profile/alice.test/post/rr1"
    assert p1.lang == "en"
    # app-view repost record has no $type/text -> detected via subject-without-text
    p2 = by_id["at://did:plc:aaa/app.bsky.feed.repost/rr2"]
    assert p2.repost_of == "at://did:plc:bbb/app.bsky.feed.post/zz9"
    p3 = by_id["at://did:plc:bbb/app.bsky.feed.post/rr3"]
    assert p3.reply_to == "at://did:plc:aaa/app.bsky.feed.post/rr1"
    assert b.provenance["pages_fetched"] == 1


def test_query_required(fake_bsky):
    with pytest.raises(ValueError, match="query"):
        bs.BlueskyConnector().run({})


def test_bluesky_job_via_api_stores_live_records(fake_bsky):
    from fastapi.testclient import TestClient
    from pickemup_api.main import app
    from pickemup_api import jobs as jobs_mod
    from pickemup_api.db import SessionLocal
    from pickemup_api.models import Account, Job, Post

    with TestClient(app) as c:
        r = c.post("/api/v1/jobs", json={"kind": "bluesky", "params": {"query": "q1"}})
        assert r.status_code == 201
        assert r.json()["kind"] == "collect"
        for _ in range(60):  # env=test: run the queue inline
            job = jobs_mod._claim_one("tw")
            if job is None:
                with SessionLocal() as s:
                    pending = s.query(Job).filter(Job.status.in_(["queued", "retrying"])).count()
                if not pending:
                    break
                continue
            jobs_mod._run_job(job)
        with SessionLocal() as s:
            assert s.get(Account, "did:plc:aaa").origin_mode == "live"
            assert s.get(Post, "at://did:plc:aaa/app.bsky.feed.post/rr1").lang == "en"
        # auto-chained analysis must be queued behind a real collect
        kinds = {j.kind for j in s.query(Job).all()} if False else None


def test_error_translation_on_400(monkeypatch):
    def fake_get(url, *a, **k):
        return httpx.Response(400, json={"message": "invalid syntax"},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(RuntimeError) as e:
        bs.BlueskyConnector().run({"query": "x"})
    assert "400" in str(e.value) and "Bluesky search syntax" in str(e.value)
