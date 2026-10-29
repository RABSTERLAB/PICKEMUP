"""Narrative propagation & earliest-observed-origin engine (priority 4).

Method: classic diffusion-origin analysis over repost/quote chains.

Hard methodological rule (directive §13): we report the EARLIEST OBSERVED
instance and never call it the true origin when collection history is
incomplete. The provenance window is always disclosed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
from datetime import datetime, timezone

from . import ALGO_VERSION
from .domain import PostObs, domain_of, post_timestamp
from .similarity import minhash, minhash_jaccard

SIM_STRONG = 0.55     # same narrative instance
SIM_VARIANT = 0.35    # observed narrative variation / earlier phrasing


@dataclass
class NarrativeGroup:
    group_id: str
    posts: list[PostObs]
    first_observed: PostObs | None
    earlier_variants: list[PostObs]
    domains: list[str]
    hashtags: list[str]
    cascade: list[dict]            # time-ordered propagation steps
    amplifiers: list[str]          # author ids whose posts are most reposted within group
    coverage_gaps: list[str]
    caveats: list[str]
    algo_version: str = ALGO_VERSION


def group_narrative(query_posts: list[PostObs], corpus: list[PostObs] | None = None,
                    collection_started: str | None = None) -> list[NarrativeGroup]:
    """Cluster near-duplicate posts into narrative instances and order cascades."""
    posts = [p for p in query_posts if post_timestamp(p)]
    corpus = corpus or []
    mh = {p.post_id: minhash(p.text) for p in posts}
    g = nx.Graph()
    for p in posts:
        g.add_node(p.post_id)
    # similarity edges
    ids = list(posts)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if minhash_jaccard(mh[a.post_id], mh[b.post_id]) >= SIM_STRONG:
                g.add_edge(a.post_id, b.post_id, kind="similar_content")
    # repost/quote/reply chains are structural edges (from query posts AND corpus)
    pmap = {p.post_id: p for p in corpus}
    pmap.update({p.post_id: p for p in posts})
    # Fixed point: chains must reach full depth regardless of input order;
    # a single pass dropped intermediates when the corpus was unordered.
    pending = list(posts) + list(corpus)
    progress = True
    while progress:
        progress = False
        for p in pending:
            for parent, kind in ((p.repost_of, "repost"), (p.quote_of, "quote"),
                                 (p.reply_to, "reply")):
                if parent in pmap and (p.post_id in g.nodes or parent in g.nodes):
                    if parent not in g.nodes or p.post_id not in g.nodes:
                        progress = True
                    g.add_node(p.post_id)
                    g.add_node(parent)
                    g.add_edge(p.post_id, parent, kind=kind)

    groups: list[NarrativeGroup] = []
    for gi, comp in enumerate(nx.connected_components(g)):
        members = sorted((pmap[k] for k in comp if k in pmap and post_timestamp(pmap[k])),
                         key=post_timestamp)  # timestampless corpus parents can't join a cascade
        if not members:
            continue
        first = members[0]
        # earlier variants: similar-but-not-identical posts in the wider corpus
        variants = []
        if corpus:
            if first.post_id not in mh:  # corpus-origin group: sign it too
                mh[first.post_id] = minhash(first.text)
            for q in corpus:
                if q.post_id in comp or not post_timestamp(q):
                    continue
                if post_timestamp(q) >= post_timestamp(first):
                    continue
                j = minhash_jaccard(mh.get(first.post_id, ()), minhash(q.text))
                if SIM_VARIANT <= j < SIM_STRONG:
                    variants.append(q)
        by_target: dict[str, int] = {}
        cascade = []
        for p in members:
            cascade.append({"post_id": p.post_id, "author_id": p.author_id,
                            "at": post_timestamp(p).isoformat(),
                            "kind": "repost" if p.repost_of else "original"})
            if p.repost_of and p.repost_of in pmap:
                # amplifiers are AUTHORS whose posts get reposted (contract of
                # NarrativeGroup + reporting render), not parent post ids
                tgt_author = pmap[p.repost_of].author_id
                by_target[tgt_author] = by_target.get(tgt_author, 0) + 1
        ampl = sorted(by_target, key=by_target.get, reverse=True)[:5]
        gaps = []
        if collection_started:
            ft = post_timestamp(first)
            try:
                cs = datetime.fromisoformat(str(collection_started).replace("Z", "+00:00"))
            except ValueError:
                cs = None
            if ft and cs:
                if ft.tzinfo is None:
                    ft = ft.replace(tzinfo=timezone.utc)
                if cs.tzinfo is None:
                    cs = cs.replace(tzinfo=timezone.utc)
                if ft <= cs:
                    gaps.append("earliest post in this narrative is at/inside the collection start — the true "
                                "origin may predate observation")
        gaps.append("coverage limited to imported/collected records; deleted or access-restricted posts are absent")
        caveats = ["earliest OBSERVED instance only — not proof of original authorship",
                   "similarity links are inferred, repost/quote links are observed"]
        groups.append(NarrativeGroup(
            group_id=f"N{gi + 1}", posts=members, first_observed=first,
            earlier_variants=variants[:10],
            domains=sorted({domain_of(u) for p in members for u in p.urls if u}),
            hashtags=sorted({h.lower() for p in members for h in p.hashtags}),
            cascade=cascade, amplifiers=ampl, coverage_gaps=gaps, caveats=caveats))
    def _first_ts(gr):  # None-safe + tz-safe: never mix int and datetime in one key
        t = gr.first_observed and post_timestamp(gr.first_observed)
        return float("-inf") if t is None else t.timestamp()
    groups.sort(key=_first_ts)
    return groups


def earliest_observed(posts: list[PostObs]) -> PostObs | None:
    ts = [p for p in posts if post_timestamp(p)]
    return min(ts, key=post_timestamp) if ts else None
