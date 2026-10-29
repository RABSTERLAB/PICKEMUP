"""Lightweight domain types shared by all engines (DB-agnostic).

Engines consume these plain dataclasses; persistence layers map ORM rows to
them. `origin_mode` ('synthetic' | 'fixture' | 'live') must always be carried
so the UI can label demo data honestly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AccountObs:
    account_id: str
    username: str
    display_name: str = ""
    created_at: datetime | None = None
    followers: int | None = None
    following: int | None = None
    verified: bool | None = None
    default_profile: bool | None = None
    origin_mode: str = "fixture"  # connectors must pass their batch origin explicitly
    ground_truth_label: str | None = None  # only populated for fixtures
    region: str | None = None              # profile-location-derived or estimated macro-region code

    def ref(self) -> str:
        return f"account:{self.account_id}"


@dataclass
class PostObs:
    post_id: str
    author_id: str
    text: str = ""
    created_at: datetime | None = None
    collected_at: datetime | None = None
    lang: str | None = None
    hashtags: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    repost_of: str | None = None      # post_id this post reposts
    quote_of: str | None = None       # post_id this post quotes
    reply_to: str | None = None
    retweet_count: int | None = None
    origin_mode: str = "fixture"  # connectors must pass their batch origin explicitly
    source_url: str | None = None

    def ref(self) -> str:
        return f"post:{self.post_id}"


def domain_of(url: str) -> str:
    """Extract registrable-ish host from a URL, defensively.

    Strips userinfo and port (credentials must never land in entity values)
    and returns '' for anything that is not host-shaped."""
    u = (url or "").strip().lower()
    if "://" in u:
        u = u.split("://", 1)[1]
    elif u.startswith("//"):
        u = u[2:]
    host = u.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    if not host or "." not in host or any(c.isspace() for c in host):
        return ""
    return host


def post_timestamp(p: PostObs) -> datetime | None:
    return p.created_at or p.collected_at
