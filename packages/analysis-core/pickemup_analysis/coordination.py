"""Coordinated-network detection (PICKEMUP flagship, priority 2).

Method lineage: academic coordination-detection research (co-repost /
co-hashtag / co-mention edges) + temporal synchronization analysis +
shared-URL diffusion analysis.

Edge rules are designed so NORMAL breaking-news co-coverage does NOT
auto-qualify as coordination:
  * shared-URL signal uses EXACT normalized URLs (article identity), not
    domains; same-domain is a separate weak signal that can never gate alone.
  * corpus-popular artifacts (trending tags, major platforms) are excluded
    from set-similarity signals.
  * an edge requires one of:
      e1  synced reposts of the same source within the repost window
      e2  strong temporal sync of near-duplicate content AND a shared
          non-popular artifact or near-duplicate content
      e3  exact-URL sharing AND near-duplicate content
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import ALGO_VERSION, VERDICT_HUMAN_COORDINATED_POSSIBLE, VERDICT_INDETERMINATE
from .domain import AccountObs, PostObs, domain_of, post_timestamp
from .similarity import minhash, minhash_jaccard

DEFAULTS = dict(
    set_threshold=0.25,
    url_threshold=0.2,
    text_signal=0.35,       # near-duplicate level for 'similar_content' signal
    strong_signal=0.6,
    sync_window_s=900,      # 15 min
    sync_threshold=0.3,
    repost_window_s=1800,   # 30 min
    popular_share=0.3,      # >=30% of active accounts => corpus-popular
)


def norm_url(u: str) -> str:
    try:
        s = urlsplit(u if "://" in u else "http://" + u)
        path = (s.path or "").rstrip("/")
        return (s.netloc.lower().removeprefix("www.") + path).lower()
    except Exception:
        return u.strip().lower()


@dataclass
class PairEvidence:
    account_a: str
    account_b: str
    signals: dict[str, float]
    evidence_post_ids: list[str]
    edge: bool
    rule: str | None
    why: str


@dataclass
class ClusterReport:
    cluster_id: str
    members: list[str]
    accounts: int
    edge_count: int
    shared_domains: list[str]  # NOT full URLs: registrable hosts (see graph kinds)
    shared_hashtags: list[str]
    signal_mix: dict[str, int]
    time_range: tuple[str, str] | None
    central_accounts: list[str]
    amplifiers: list[str]
    organic_explanations: list[str]
    confidence: float
    verdict: str
    caveats: list[str] = field(default_factory=list)
    algo_version: str = ALGO_VERSION


class AccountActivity:
    def __init__(self, account: AccountObs, posts: list[PostObs]):
        self.account = account
        self.posts = sorted((p for p in posts if post_timestamp(p)), key=post_timestamp)
        originals = [p for p in self.posts if not p.repost_of]
        self.urls = {norm_url(u) for p in originals for u in p.urls if u}
        self.domains = {domain_of(u) for p in originals for u in p.urls if u}
        self.hashtags = {h.lower() for p in originals for h in p.hashtags}
        self.mentions = {m.lower() for p in originals for m in p.mentions}
        self.reposted_srcs = {p.repost_of for p in self.posts if p.repost_of}
        self.originals = originals
        self.mh = [(p, minhash(p.text)) for p in originals if p.text]


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def _temporal_sync(ak: AccountActivity, bk: AccountActivity, cfg) -> float:
    """Fraction of A's originals that have a NEAR-DUPLICATE B original within
    ±sync_window. Coordination = same content at the same time; organic event
    coverage = same topic at random times."""
    win = cfg["sync_window_s"]
    hits = 0
    examined = 0
    for p, mp in ak.mh[:40]:
        examined += 1
        tp = post_timestamp(p).timestamp()
        for q, mq in bk.mh[:40]:
            if abs(post_timestamp(q).timestamp() - tp) <= win and \
                    minhash_jaccard(mp, mq) >= cfg["text_signal"]:
                hits += 1
                break
    # denominator = examined slice only (mixing :40 numerator with full len
    # silently diluted the signal for accounts over 40 originals)
    return hits / examined if examined else 0.0


def analyze_pair(ak: AccountActivity, bk: AccountActivity, cfg=DEFAULTS,
                 popular_urls: set | None = None, popular_tags: set | None = None) -> PairEvidence:
    popular_urls = popular_urls or set()
    popular_tags = popular_tags or set()
    signals: dict[str, float] = {}
    ev: list[str] = []

    shared_urls = (ak.urls & bk.urls) - popular_urls
    if shared_urls:
        s = _jaccard(ak.urls - popular_urls, bk.urls - popular_urls)
        if s >= cfg["url_threshold"]:
            signals["shared_urls"] = round(s, 3)
            ev += [p.post_id for p in ak.originals
                   if any(norm_url(u) in shared_urls for u in p.urls)][:3]

    shared_tags = (ak.hashtags & bk.hashtags) - popular_tags
    if shared_tags:
        s = _jaccard(ak.hashtags - popular_tags, bk.hashtags - popular_tags)
        if s >= cfg["set_threshold"]:
            signals["shared_hashtags"] = round(s, 3)

    # domain-level coincidence — recorded, NEVER edge-sufficient alone
    sd = _jaccard(ak.domains, bk.domains)
    if sd >= 0.4:
        signals["same_domain_weak"] = round(sd, 3)

    best = 0.0
    for _, m1 in ak.mh[:40]:
        for _, m2 in bk.mh[:40]:
            j = minhash_jaccard(m1, m2)
            if j > best:
                best = j
    if best >= cfg["text_signal"]:
        signals["similar_content"] = round(best, 3)

    t_sync = (_temporal_sync(ak, bk, cfg) + _temporal_sync(bk, ak, cfg)) / 2
    if t_sync >= cfg["sync_threshold"]:
        signals["temporal_sync"] = round(t_sync, 3)

    shared_srcs = ak.reposted_srcs & bk.reposted_srcs
    if shared_srcs:
        co = 0
        for src in shared_srcs:
            ta = [post_timestamp(p) for p in ak.posts if p.repost_of == src]
            tb = [post_timestamp(p) for p in bk.posts if p.repost_of == src]
            if any(abs((a - b).total_seconds()) <= cfg["repost_window_s"] for a in ta for b in tb):
                co += 1
        if co and co / len(shared_srcs) >= 0.5:
            signals["synced_reposts"] = round(co / len(shared_srcs), 3)
            for src in shared_srcs:
                ev += [p.post_id for p in ak.posts if p.repost_of == src][:2]

    # ---- composite edge rules ----
    rule = None
    if "synced_reposts" in signals:
        rule = "e1_synced_reposts"
    # temporal_sync was itself minted from a minhash>=text_signal pair, so the
    # similar_content clause below was ALWAYS true — corroboration must be
    # independent evidence (shared tag/url), not the same signature restated.
    elif "temporal_sync" in signals and ("shared_hashtags" in signals or "shared_urls" in signals):
        rule = "e2_synced_duplicate_content"
    elif "shared_urls" in signals and "similar_content" in signals:
        rule = "e3_shared_url_and_content"
    edge = rule is not None
    why = "; ".join(f"{k}={v}" for k, v in sorted(signals.items())) or "no shared-behaviour signal above threshold"
    if not edge and signals:
        why += " — too weak/standalone to support a coordination edge"
    return PairEvidence(ak.account.account_id, bk.account.account_id, signals, ev[:6], edge, rule, why)


def build_edges(activities: list[AccountActivity], cfg=DEFAULTS,
                popular_urls=None, popular_tags=None):
    byid = {a.account.account_id: a for a in activities}
    cand: set[frozenset] = set()
    by_tag: dict[str, set[str]] = defaultdict(set)
    by_url: dict[str, set[str]] = defaultdict(set)
    by_src: dict[str, set[str]] = defaultdict(set)
    for a in activities:
        for h in a.hashtags:
            by_tag[h].add(a.account.account_id)
        for u in a.urls:
            by_url[u].add(a.account.account_id)
        for s in a.reposted_srcs:
            by_src[s].add(a.account.account_id)
    for pool_map in (by_tag, by_url, by_src):
        for pool in pool_map.values():
            pool_l = sorted(pool)
            if 2 <= len(pool_l) <= 400:
                for i, x in enumerate(pool_l):
                    for y in pool_l[i + 1:]:
                        cand.add(frozenset((x, y)))
    allp, edges = [], []
    for pair in cand:
        x, y = sorted(pair)
        pe = analyze_pair(byid[x], byid[y], cfg, popular_urls, popular_tags)
        allp.append(pe)
        if pe.edge:
            edges.append(pe)
    return edges, allp


def cluster_accounts(activities: list[AccountActivity], edges: list[PairEvidence],
                     popular_hashtags: set[str], popular_domains: set[str],
                     post_author: dict[str, str] | None = None, total_accounts: int = 0):
    """Group edges into clusters (Louvain) and emit explainable reports."""
    import networkx as nx
    g = nx.Graph()
    for a in activities:
        g.add_node(a.account.account_id)
    for e in edges:
        w = max((v for k, v in e.signals.items() if k != "same_domain_weak"), default=1.0)
        attrs = {k: v for k, v in e.signals.items()}
        g.add_edge(e.account_a, e.account_b, weight=w, rules=[e.rule], **attrs)
    reports: list[ClusterReport] = []
    if g.number_of_edges() == 0:
        return reports
    try:
        comms = [c for c in nx.community.louvain_communities(g, weight="weight", seed=42) if len(c) >= 3]
    except Exception:
        comms = [c for c in nx.connected_components(g) if len(c) >= 3]
    byid = {a.account.account_id: a for a in activities}
    post_author = post_author or {}
    for ci, comm0 in enumerate(comms):
        comm = set(comm0)
        sub = g.subgraph(comm)
        tags: Counter = Counter()
        doms: Counter = Counter()
        tag_users: dict[str, set] = {}
        dom_users: dict[str, set] = {}
        tmin = tmax = None
        for aid in comm:
            a = byid[aid]
            tags.update(a.hashtags)
            doms.update(a.domains)
            for h in set(a.hashtags):
                tag_users.setdefault(h, set()).add(aid)
            for d in set(a.domains):
                dom_users.setdefault(d, set()).add(aid)
            for p in a.posts:
                t = post_timestamp(p)
                tmin = t if tmin is None or t < tmin else tmin
                tmax = t if tmax is None or t > tmax else tmax
        sig_mix: Counter = Counter()
        rules: Counter = Counter()
        for _, _, dd in sub.edges(data=True):
            for k, v in dd.items():
                if k == "weight":
                    continue
                if k == "rules" and isinstance(v, list):
                    rules.update(v)
                else:
                    sig_mix[k] += 1
        rep_in: Counter = Counter()
        for aid in comm:
            for p in byid[aid].posts:
                if p.repost_of and post_author.get(p.repost_of):
                    rep_in[post_author[p.repost_of]] += 1
        amp = [x for x, _ in sorted(
            ((x, rep_in.get(x, 0) * 2 + sum((p.retweet_count or 0) for p in byid[x].posts))
             for x in comm), key=lambda kv: -kv[1])[:5]]
        centr = sorted(comm, key=lambda x: sub.degree(x), reverse=True)[:5]
        organic = []
        if any(t in popular_hashtags for t in tags):
            organic.append("cluster shares a corpus-popular hashtag — consistent with organic event coverage")
        if any(d in popular_domains for d in doms):
            organic.append("shared domain is common corpus-wide (popular platform/news site)")
        if total_accounts and len(comm) >= 0.5 * total_accounts:
            organic.append(f"cluster covers {len(comm)}/{total_accounts} active accounts — mass participation"
                           " is usually organic; verify before treating as coordinated")
        if set(sig_mix) <= {"same_domain_weak"}:
            organic.append("edges rest on domain-level coincidence only")
        n_strong = len([k for k in sig_mix
                        if k in ("synced_reposts", "temporal_sync", "shared_urls", "similar_content")])
        conf = round(min(0.9, 0.25 + 0.15 * n_strong + 0.02 * min(20, sub.number_of_edges())), 2)
        verdict = VERDICT_HUMAN_COORDINATED_POSSIBLE if n_strong >= 2 else VERDICT_INDETERMINATE
        if organic and conf < 0.7 and verdict == VERDICT_HUMAN_COORDINATED_POSSIBLE:
            verdict = VERDICT_INDETERMINATE  # plausible organic explanation + weak evidence
        reports.append(ClusterReport(
            cluster_id=f"C{ci + 1}", members=sorted(comm), accounts=len(comm),
            edge_count=sub.number_of_edges(),
            # "shared" must mean used by >=2 cluster members — single-account
            # artifacts would mislead the analyst-facing cluster report
            shared_domains=sorted((d for d in dom_users if len(dom_users[d]) >= 2),
                               key=lambda d: (-len(dom_users[d]), -doms[d]))[:8],
            shared_hashtags=sorted((t for t in tag_users if len(tag_users[t]) >= 2),
                                   key=lambda t: (-len(tag_users[t]), -tags[t]))[:8],
            signal_mix=dict(sig_mix),
            time_range=(tmin.isoformat(), tmax.isoformat()) if tmin and tmax else None,
            central_accounts=centr, amplifiers=amp,
            organic_explanations=organic,
            confidence=conf, verdict=verdict,
            caveats=["edges denote observed behavioral similarity, NOT proven common control"],
        ))
    return reports


def detect_coordination(accounts: list[AccountObs], posts: list[PostObs], cfg=DEFAULTS):
    acts, by_acct = [], defaultdict(list)
    for p in posts:
        by_acct[p.author_id].append(p)
    amap = {a.account_id: a for a in accounts}
    for aid, plist in by_acct.items():
        if aid in amap:
            acts.append(AccountActivity(amap[aid], plist))
    if len(acts) < 3:
        return [], [], acts
    post_author = {p.post_id: p.author_id for p in posts}
    n = len(acts)
    tag_counts: Counter = Counter()
    url_counts: Counter = Counter()
    dom_counts: Counter = Counter()
    for a in acts:
        tag_counts.update(a.hashtags)
        url_counts.update(a.urls)
        dom_counts.update(a.domains)
    cut = max(3, int(n * cfg["popular_share"]))
    popular_tags = {t for t, c in tag_counts.items() if c >= cut}
    popular_domains = {d for d, c in dom_counts.items() if c >= cut}
    popular_urls = {u for u, c in url_counts.items() if c >= cut}
    edges, allp = build_edges(acts, cfg, popular_urls, popular_tags)
    reports = cluster_accounts(acts, edges, popular_tags, popular_domains, post_author, total_accounts=n)
    return reports, edges, acts
