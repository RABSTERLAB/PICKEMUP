"""Bluesky connector — AT Protocol public API (searchPosts).

No credentials required: app.bsky.feed.searchPosts is public and free, which
makes Bluesky the zero-cost live path of this product. Honest limits: it is a
SEARCH api (relevance-ranked, cursor-paginated, public records only); deleted
posts are gone; chronological firehose (jetstream) is a different adapter we
do not pretend to be.
"""
from __future__ import annotations

import time

import httpx

from pickemup_analysis.domain import AccountObs, PostObs

from . import BudgetGuard, Connector, ConnectorManifest, IngestBatch

# app view (api.bsky.app) is the public unauthenticated endpoint; bsky.social
# now answers searchPosts with 401 "Authentication Required" for anonymous callers.
BASE = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"


class BlueskyConnector(Connector):
    manifest = ConnectorManifest(
        name="bluesky", platform="bluesky", auth_required=False,
        capabilities=["search", "reposts", "replies", "hashtag_facets"],
        rate_limit_desc="public search endpoint; back off on 429",
        cost_model="free — no API key required",
        data_freshness="near-live (indexed shortly after posting)",
        retention_rule="public posts until deleted; recent window only",
        coverage_gaps=[
            "search-relevance view, not a full chronological firehose",
            "deleted posts and edited history unavailable",
            "DMs and non-public records out of scope",
        ],
    )

    def available(self):
        return True, "live Bluesky AT Public API — no credentials required"

    def run(self, params: dict, budget: BudgetGuard | None = None) -> IngestBatch:
        query = params.get("query", "")
        if not query:
            raise ValueError("bluesky collect requires params.query")
        max_pages = int(params.get("max_pages", 1))
        limit = min(int(params.get("page_size", 50)), 100)
        batch = IngestBatch(origin_mode="live",
                            provenance={"source_kind": "bluesky_searchposts_appview", "query": query,
                                        "started_at": self.now().isoformat()})
        headers = {"Accept": "application/json"}
        cursor = params.get("cursor")
        seen_posts, seen_accts = set(), set()
        pages = 0
        for _ in range(max_pages):
            q = {"q": query, "limit": limit, "sort": params.get("sort", "latest")}
            if cursor:
                q["cursor"] = cursor
            r = httpx.get(BASE, params=q, headers=headers, timeout=30)
            if r.status_code == 429:
                time.sleep(30)
                continue
            if r.status_code >= 400:
                try:
                    detail = str(r.json().get("message") or r.json().get("detail") or "").strip()[:180]
                except Exception:
                    detail = r.text[:180]
                hint = {400: "bad query — Bluesky search syntax differs from X",
                        401: "unexpected auth prompt — endpoint should be public",
                        413: "query too broad — narrow it",
                        502: "upstream Bluesky hiccup — retry shortly"}.get(r.status_code, "Bluesky API error")
                raise RuntimeError(f"Bluesky HTTP {r.status_code} — {hint}" + (f": {detail}" if detail else ""))
            pages += 1
            data = r.json()
            for post in data.get("posts", []):
                author = post.get("author") or {}
                aid = author.get("did") or author.get("handle")
                if not aid:
                    continue
                if aid not in seen_accts:
                    seen_accts.add(aid)
                    batch.accounts.append(
                        _account(author, self.now()))
                rec = post.get("record") or {}
                rtype = rec.get("$type", "")
                pid = post.get("uri", "")
                if not pid or pid in seen_posts:
                    continue
                seen_posts.add(pid)
                rkey = pid.rsplit("/", 1)[-1]
                handle = author.get("handle") or aid
                facets = rec.get("facets") or []
                urls = [f.get("features", [{}])[0].get("uri")
                        for f in facets if _feat_type(f) == "link"]
                hashtags = [f.get("features", [{}])[0].get("tag")
                            for f in facets if _feat_type(f) == "tag"]
                mentions = [f.get("features", [{}])[0].get("did")
                            for f in facets if _feat_type(f) == "mention"]
                repost_of = reply_to = None
                if rtype == "app.bsky.feed.repost" or (not rec.get("text") and rec.get("subject")):
                    repost_of = (rec.get("subject") or {}).get("uri")
                reply = rec.get("reply") or {}
                if reply.get("parent"):
                    reply_to = (reply["parent"] or {}).get("uri")
                text = rec.get("text", "")
                if not text and repost_of:
                    text = "[repost]"
                batch.posts.append(_post(pid, aid, text, rec, handle, rkey,
                                         urls, hashtags, mentions, repost_of, reply_to,
                                         post, self.now()))
            cursor = data.get("cursor")
            if not cursor:
                break
        batch.provenance["pages_fetched"] = pages
        self._enrich_profiles(batch.accounts)
        return batch

    def _enrich_profiles(self, accounts) -> None:
        """Batched profile lookup (free public endpoint, up to 25 actors/call) to
        recover createdAt, authoritative counts and default-look detection that
        the search view omits. Best-effort: never fail a collection over it."""
        need = [a.account_id for a in accounts if str(a.account_id).startswith("did:")]
        if not need:
            return
        by_id = {a.account_id: a for a in accounts}
        try:
            for i in range(0, len(need), 25):
                r = httpx.get("https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles",
                              params=[("actors", d) for d in need[i:i + 25]], timeout=30)
                if r.status_code != 200:
                    return
                data = r.json()
                profs = data.get("profiles", [data]) if isinstance(data, dict) else [data]
                for pr in profs or []:
                    a = by_id.get(pr.get("did"))
                    if not a:
                        continue
                    if pr.get("createdAt"):
                        a.created_at = _parse_iso(pr["createdAt"])
                    if pr.get("followersCount") is not None:
                        a.followers = pr["followersCount"]
                    if pr.get("followsCount") is not None:
                        a.following = pr["followsCount"]
                    a.default_profile = bool(not pr.get("avatar") and not pr.get("description"))
        except Exception:
            return


def _feat_type(facet) -> str:
    feats = facet.get("features") or [{}]
    t = feats[0].get("$type", "")
    return t.rsplit("#", 1)[-1].rsplit(".", 1)[-1]


def _parse_iso(v):
    from datetime import datetime, timezone
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _account(author, now):
    did = author.get("did") or author.get("handle", "")
    return AccountObs(
        account_id=did, username=author.get("handle") or did,
        display_name=author.get("displayName"),
        verified=bool(author.get("verified")),
        followers=author.get("followersCount"), following=author.get("followsCount"),
        origin_mode="live")


def _post(pid, aid, text, rec, handle, rkey, urls, hashtags, mentions,
          repost_of, reply_to, post, now):
    return PostObs(
        post_id=pid, author_id=aid, text=text,
        created_at=_parse_iso(rec.get("createdAt")) or now, collected_at=now,
        lang=(rec.get("langs") or [None])[0],
        hashtags=[h for h in hashtags if h], urls=[u for u in urls if u],
        mentions=[m for m in mentions if m],
        repost_of=repost_of, quote_of=None, reply_to=reply_to,
        retweet_count=post.get("repostCount"),
        origin_mode="live",
        source_url=f"https://bsky.app/profile/{handle}/post/{rkey}")
