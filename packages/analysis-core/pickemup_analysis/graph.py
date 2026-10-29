"""Graph construction & snapshot export for the Network Explorer.

Nodes/edges carry type + evidence so the UI can render the required legend
distinguishing OBSERVED relationships (reposted/quoted/mentioned) from
INFERRED similarity (shared-url, similar-content, temporal-sync).
"""
from __future__ import annotations

from dataclasses import dataclass

import logging

import networkx as nx

_log = logging.getLogger("pickemup.graph")

from . import ALGO_VERSION
from .domain import AccountObs, PostObs, post_timestamp

OBSERVED_EDGE_KINDS = {"reposted", "quoted", "mentioned", "replied"}
# must equal the signal keys coordination.py actually emits (was singular +
# a phantom shared_mentions -> legend/contract mismatch flagged by OCR audit)
INFERRED_EDGE_KINDS = {"shared_urls", "shared_hashtags", "same_domain_weak",
                       "similar_content", "temporal_sync", "synced_reposts"}


def _pagerank_pure(g, alpha=0.85, iters=50):
    n = g.number_of_nodes()
    if not n:
        return {}
    x = {v: 1.0 / n for v in g}
    for _ in range(iters):
        dangling = sum(x[u] for u in g if g.degree(u) == 0)
        share = alpha * dangling / n
        nx_ = {v: (1 - alpha) / n + share for v in g}  # redistribute dangling rank mass
        for u in g:
            d = g.degree(u)
            if not d:
                continue
            for v in g[u]:
                nx_[v] += alpha * x[u] / d
        x = nx_
    return x


@dataclass
class GraphSnapshot:
    nodes: list[dict]
    edges: list[dict]
    clusters: dict[str, list[str]]     # cluster_id -> member account ids
    stats: dict
    algo_version: str = ALGO_VERSION

    def to_dict(self) -> dict:
        return {"nodes": self.nodes, "edges": self.edges, "clusters": self.clusters,
                "stats": self.stats, "algo_version": self.algo_version,
                "legend": {"observed": sorted(OBSERVED_EDGE_KINDS), "inferred": sorted(INFERRED_EDGE_KINDS)}}


def account_graph(accounts: list[AccountObs], posts: list[PostObs], edges_evidence,
                  cluster_reports) -> GraphSnapshot:
    g = nx.Graph()
    amap = {a.account_id: a for a in accounts}
    involved = {e.account_a for e in edges_evidence} | {e.account_b for e in edges_evidence}
    involved |= {m for r in cluster_reports for m in r.members}
    for aid in involved:
        a = amap.get(aid)
        g.add_node(aid, type="account", label=(a.username if a else aid),
                   origin_mode=(a.origin_mode if a else "unknown"))
    for e in edges_evidence:
        if e.account_a not in g or e.account_b not in g:
            continue
        kinds = [k for k in e.signals]
        g.add_edge(e.account_a, e.account_b, kinds=kinds, weight=max(e.signals.values(), default=1.0),
                   evidence=list(e.evidence_post_ids or []),  # never alias the source record
                   observed=any(k in OBSERVED_EDGE_KINDS for k in kinds))
    # Observed interactions (repost / quote / reply / mention) join the map even
    # when neither endpoint scored a coordination edge — a fresh keyword sweep
    # should light up "who talks to whom", not stay blank until engines agree.
    pmap = {p.post_id: p for p in posts}
    def _inter(a: str, b: str, kind: str, pid: str):
        if a not in g and a in amap:
            g.add_node(a, type="account", label=amap[a].username, origin_mode=amap[a].origin_mode)
        if b not in g and b in amap:
            g.add_node(b, type="account", label=amap[b].username, origin_mode=amap[b].origin_mode)
        if a not in g or b not in g or a == b:
            return
        if g.has_edge(a, b):
            d = g.edges[a, b]
            if kind not in d["kinds"]:
                d["kinds"].append(kind)
            d["observed"] = True
            d["weight"] = d.get("weight", 1.0) + 1.0
            d["evidence"] = (d["evidence"] + [pid])[:8]
        else:
            g.add_edge(a, b, kinds=[kind], weight=2.0, evidence=[pid], observed=True)

    for p in posts:
        for ref, kind in ((getattr(p, "repost_of", None), "reposted"),
                          (getattr(p, "quote_of", None), "quoted"),
                          (getattr(p, "reply_to", None), "replied")):
            if ref and ref in pmap:
                _inter(p.author_id, pmap[ref].author_id, kind, p.post_id)
        for m in (getattr(p, "mentions", None) or []):
            mu = str(m).lstrip("@").strip()
            tgt = mu if mu in amap else next((a.account_id for a in accounts
                                              if a.username and a.username.lower() == mu.lower()), None)
            if tgt:
                _inter(p.author_id, tgt, "mentioned", p.post_id)

    deg = dict(g.degree())
    try:
        pr = nx.pagerank(g, alpha=0.85) if g.number_of_edges() else {n: 0.0 for n in g}
    except Exception as e:  # scipy-free fallback: plain power iteration
        _log.warning("pagerank fallback: %s", e)
        pr = _pagerank_pure(g, alpha=0.85)
    try:
        bet = nx.betweenness_centrality(g, seed=42) if g.number_of_edges() <= 800 else deg
    except Exception as e:
        _log.warning("betweenness fallback: %s", e)
        bet = deg
    cluster_of: dict[str, str] = {}
    for r in cluster_reports:
        for m in r.members:
            cluster_of[m] = r.cluster_id
    nodes = []
    for n in g:
        nodes.append({"id": n, "label": g.nodes[n].get("label", n), "type": g.nodes[n].get("type", "account"),
                      "degree": deg.get(n, 0), "pagerank": round(pr.get(n, 0.0), 5),
                      "betweenness": round(float(bet.get(n, 0) if not hasattr(bet.get(n, 0), "values") else 0), 5),
                      "cluster": cluster_of.get(n), "origin_mode": g.nodes[n].get("origin_mode"),
                      "size": 6 + min(24, deg.get(n, 0) * 2)})
    edges = [{"source": u, "target": v, "kinds": d["kinds"], "weight": d["weight"],
              "evidence": d.get("evidence", []), "observed": d.get("observed", False)}
             for u, v, d in g.edges(data=True)]
    clusters = {r.cluster_id: r.members for r in cluster_reports}
    stats = {"nodes": g.number_of_nodes(), "edges": g.number_of_edges(),
             "clusters": len(cluster_reports),
             "density": round(nx.density(g), 4) if g.number_of_nodes() else 0.0}
    return GraphSnapshot(nodes=nodes, edges=edges, clusters=clusters, stats=stats)


def temporal_frames(snapshot: GraphSnapshot, posts: list[PostObs], bucket_minutes: int = 60) -> list[dict]:
    """Playback frames: edges appear when their supporting posts exist."""
    from datetime import timedelta
    ts = [post_timestamp(p) for p in posts if post_timestamp(p)]
    if not ts:
        return []
    t0, t1 = min(ts), max(ts)
    bucket_minutes = max(1, int(bucket_minutes or 60))
    # frame-count guard: a huge span at 1-minute buckets could exhaust memory
    span_min = (t1 - t0).total_seconds() / 60.0
    if span_min / bucket_minutes > 2000:
        bucket_minutes = int(span_min / 2000) + 1
    step = timedelta(minutes=bucket_minutes)
    pmap = {p.post_id: p for p in posts}
    ts_cache = {pid: post_timestamp(p) for pid, p in pmap.items()}
    frames, t = [], t0
    while t <= t1 + step:
        active_edges = []
        for e in snapshot.edges:
            ev_times = [ts for x in e["evidence"] if (ts := ts_cache.get(x))]
            # edges with NO resolvable timestamp stay visible (else they silently
            # vanish from the whole playback); the caveat labels them undated
            if not ev_times or min(ev_times) <= t:
                active_edges.append(e["source"] + "|" + e["target"])
        frames.append({"at": t.isoformat(), "active_edges": active_edges,
                       "edge_count": len(active_edges)})
        t += step
    return frames
