"""X (Twitter) API v2 connector.

Two classes share one interface:
  XApiConnector   — real HTTP adapter (inert without X_BEARER_TOKEN; refuses to
                    pretend otherwise). Implements search/lookup pagination,
                    rate-limit backoff and BudgetGuard charging per call.
  MockXConnector  — offline stand-in exercising the identical interface with a
                    small synthetic fixture, loudly labeled origin_mode='mock'.

Endpoint shapes follow docs.x.com/x-api; verify against current vendor docs
X_API_ASSESSMENT.md before enabling live use.
"""
from __future__ import annotations

import os
import time

from pickemup_analysis.domain import AccountObs, PostObs

from . import BudgetGuard, Connector, ConnectorManifest, IngestBatch

BASE = "https://api.x.com/2"


class XApiConnector(Connector):
    manifest = ConnectorManifest(
        name="x_api_v2", platform="x",
        capabilities=["search", "account_lookup", "posts_lookup", "user_timeline",
                      "reposts", "quotes"],
        auth_required=True,
        rate_limit_desc="per-endpoint limits; honors x-rate-limit-reset with backoff",
        cost_model="pay-per-use (~$0.005/post read, ~$0.010/user read — verify against pricing page)",
        coverage_gaps=["recent search window only on basic tiers; deleted/access-restricted posts absent"],
        retention_rule="delete ingested content when source post/account is deleted (X developer policy)",
        data_freshness="live",
    )

    def __init__(self, token: str | None = None):
        # explicit token (saved via UI) wins over process env / .env fallback
        self.token = (token if token is not None else os.environ.get("X_BEARER_TOKEN", "")).strip()

    def available(self):
        if not self.token:
            return False, "X_BEARER_TOKEN not set — configure it in .env or use the mock connector"
        return True, "live X API v2 adapter"

    def run(self, params: dict, budget: BudgetGuard | None = None) -> IngestBatch:
        ok, why = self.available()
        if not ok:
            raise RuntimeError(f"x_api unavailable: {why}")
        import httpx
        query = params.get("query")
        max_pages = int(params.get("max_pages", 5))
        since = params.get("since_id")
        headers = {"Authorization": f"Bearer {self.token}"}
        batch = IngestBatch(origin_mode="live",
                            provenance={"source_kind": "x_api_v2", "query": query,
                                        "started_at": self.now().isoformat()})
        seen_posts, seen_accts = set(), set()
        next_token = since
        pages_fetched = 0
        for _page in range(max_pages):
            # per-page cost gate BEFORE the call
            if budget:
                est = budget.estimate(post_reads=100, user_reads=20)
                if est.capped_by:
                    batch.provenance["stopped_reason"] = f"budget cap: {est.capped_by}"
                    break
            pages_fetched += 1
            r = httpx.get(f"{BASE}/tweets/search/recent", headers=headers,
                          params={"query": query, "max_results": 100,
                                  "tweet.fields": "created_at,entities,referenced_tweets,public_metrics,author_id,lang",
                                  "next_token": next_token}, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("x-rate-limit-reset", "0")) - int(time.time()) + 1
                time.sleep(min(max(wait, 1), 900))
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                    detail = str(body.get("detail") or body.get("title") or "").strip()[:180]
                except Exception:
                    detail = r.text[:180]
                hint = {401: "token invalid or revoked — regenerate at developer.x.com",
                        402: "NO CREDITS on this X project — top up at developer.x.com → Billing",
                        403: "endpoint not permitted on this access tier",
                        }.get(r.status_code, "X API error")
                raise RuntimeError(f"X HTTP {r.status_code} — {hint}" + (f": {detail}" if detail else ""))
            data = r.json()
            if budget:
                budget.charge(budget.estimate(post_reads=len(data.get("data", [])), user_reads=0))
            for t in data.get("data", []):
                aid = t["author_id"]
                if aid not in seen_accts:
                    seen_accts.add(aid)
                    batch.accounts.append(AccountObs(account_id=aid, username=t.get("username", aid),
                                                     origin_mode="live"))
                if t["id"] in seen_posts:
                    continue
                seen_posts.add(t["id"])
                ents = t.get("entities", {}) or {}
                refs = {rr["type"]: rr["id"] for rr in t.get("referenced_tweets", []) if rr.get("type") in
                        ("retweet", "quoted", "replied_to")}
                batch.posts.append(PostObs(
                    post_id=t["id"], author_id=aid, text=t.get("text", ""),
                    created_at=_iso(t.get("created_at")), collected_at=self.now(),
                    lang=t.get("lang"),
                    hashtags=[h["tag"] for h in ents.get("hashtags", [])],
                    urls=[u.get("expanded_url") or u.get("url") for u in ents.get("urls", [])],
                    mentions=[m["username"] for m in ents.get("mentions", [])],
                    repost_of=refs.get("retweet"), quote_of=refs.get("quoted"),
                    reply_to=refs.get("replied_to"),
                    retweet_count=(t.get("public_metrics") or {}).get("retweet_count"),
                    origin_mode="live", source_url=f"https://x.com/i/status/{t['id']}"))
            next_token = (data.get("meta") or {}).get("next_token")
            if not next_token:
                break
        batch.provenance["pages_fetched"] = pages_fetched
        return batch


class MockXConnector(XApiConnector):
    """Identical interface, zero network. For offline development & demos."""

    dev_only = True

    def __init__(self, token: str | None = None):  # token accepted but irrelevant offline
        super().__init__(token="mock")

    def available(self):
        return True, "MOCK MODE — synthetic records through the X connector interface"

    def run(self, params: dict, budget: BudgetGuard | None = None) -> IngestBatch:
        from .synthetic import SyntheticCampaignConnector
        b = SyntheticCampaignConnector().run({"seed": int(params.get("seed", 7))}, budget)
        b.origin_mode = "mock"
        b.provenance.update({"source_kind": "x_api_mock", "query": params.get("query"),
                             "note": "MOCK — no network call performed"})
        return b


def _iso(v):
    if not v:
        return None
    from datetime import datetime, timezone
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
