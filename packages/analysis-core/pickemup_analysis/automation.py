"""Multi-signal, explainable automation-likelihood scoring.

Anti-patterns this engine explicitly avoids (directive §10):
- Never scores "bot" from volume alone: the volume signals are only ever
  contributing context, and the verdict gate REQUIRES at least two independent
  signal families (temporal, content, network/behavioral).
- Always returns per-signal evidence so an analyst can disagree with it.
- Returns INSUFFICIENT_EVIDENCE for sparse activity instead of guessing.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import (ALGO_VERSION, VERDICT_INSUFFICIENT, VERDICT_LIKELY_AUTOMATED, VERDICT_LIKELY_HUMAN,
               VERDICT_MIXED_AUTOMATION, VERDICT_INDETERMINATE)
from .domain import PostObs, post_timestamp
from .similarity import normalize, shingles

MIN_POSTS = 5
MIN_SPAN_HOURS = 2.0

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_NUM_RE = re.compile(r"\d+")
_USER_RE = re.compile(r"@[\w\u0600-\u06ff]+")


_SLOT_TOKENS = ("sloturl", "slotn", "slotuser")


def _template(text: str) -> str:
    """Normalize away variable slots so repost-bots' message frames collapse.
    Slot tokens are word-safe (plain identifiers) because normalize() strips
    punctuation like <>."""
    t = _URL_RE.sub(" sloturl ", (text or "").lower())
    t = _USER_RE.sub(" slotuser ", t)
    t = _NUM_RE.sub(" slotn ", t)
    return normalize(t)


@dataclass
class Signal:
    name: str
    family: str          # temporal | content | behavioral | metadata
    value: float         # raw metric
    score: float         # normalized 0..1 contribution
    detail: str          # human-readable explanation w/ evidence ids


@dataclass
class AutomationResult:
    account_id: str
    verdict: str
    automation_score: float           # 0..100
    confidence: float                 # 0..1
    signals: list[Signal] = field(default_factory=list)
    evidence_post_ids: list[str] = field(default_factory=list)
    algo_version: str = ALGO_VERSION
    caveats: list[str] = field(default_factory=list)
    weak_profile: list[str] = field(default_factory=list)


def _entropy(counts: list[int]) -> float:
    total = sum(counts)
    if total <= 0:
        return 0.0
    return -sum((c / total) * math.log2(c / total) for c in counts if c) / math.log2(len(counts))


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _weak_profile_signals(meta: dict) -> list[str]:
    """Low-confidence profile cues for accounts whose post history is too sparse for
    the behavioral engine. NEVER a bot claim — an attention hint + re-collect prompt."""
    import re as _re
    from datetime import datetime as _d, timezone as _tz
    out: list[str] = []
    user = str(meta.get("username") or "")
    dn = str(meta.get("display_name") or "")
    if _re.search(r"\d{4,}", user):
        out.append("handle contains a long numeric run (auto-generated look)")
    if dn and dn.lower() == user.lower():
        out.append("display name identical to handle")
    fol, fin = meta.get("followers"), meta.get("following")
    if fol == 0 and fin in (0, None):
        out.append("no followers and no following")
    elif isinstance(fin, int) and fin >= 200 and (fol or 0) < max(10, int(fin * 0.02)):
        out.append(f"follows {fin} but has {fol or 0} followers (one-directional graph)")
    if meta.get("created_at"):
        try:
            created = _d.fromisoformat(str(meta["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=_tz.utc)
            age_d = (_d.now(_tz.utc) - created).days
            if age_d <= 14:
                out.append(f"account is only {age_d} days old")
        except ValueError:
            pass
    if meta.get("default_profile") is True:
        out.append("platform-default profile (no avatar/bio customization)")
    return out


def score_account(account_id: str, posts: list[PostObs],
                  account_meta: dict | None = None,
                  content_class: dict | None = None) -> AutomationResult:
    posts = sorted((p for p in posts if post_timestamp(p)), key=post_timestamp)
    res = AutomationResult(account_id=account_id, verdict=VERDICT_INSUFFICIENT,
                           automation_score=0.0, confidence=0.0)
    if len(posts) < MIN_POSTS:
        res.caveats.append(f"only {len(posts)} timestamped posts (< {MIN_POSTS})")
        # Sparse history must not mean "no information": grade weak profile cues so
        # operators get an honest low-confidence nudge instead of a dead end.
        res.weak_profile = _weak_profile_signals(account_meta or {})
        if res.weak_profile:
            res.verdict = VERDICT_INDETERMINATE
            res.automation_score = round(min(35.0, 8.0 * len(res.weak_profile)), 1)
            res.confidence = round(min(0.25, 0.07 * len(res.weak_profile)), 2)
            res.caveats.append("sparse data: scored on weak profile cues only — "
                               "these are NOT automation evidence")
        return res
    ts = [post_timestamp(p) for p in posts]
    span_h = (ts[-1] - ts[0]).total_seconds() / 3600.0
    if span_h < MIN_SPAN_HOURS:
        res.caveats.append(f"activity span {span_h:.1f}h < {MIN_SPAN_HOURS}h — too brief to characterize")
        return res
    res.evidence_post_ids = [p.post_id for p in posts][:50]

    sig: list[Signal] = []

    # ---- temporal family ----
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    mean_g = sum(gaps) / len(gaps)
    sd_g = _stdev(gaps)
    cv = sd_g / mean_g if mean_g else 0.0
    # clock-like regularity: fraction of gaps within 10% of the modal gap bucket
    buckets = Counter(round(math.log10(max(g, 1))) for g in gaps)
    modal_bucket, modal_n = buckets.most_common(1)[0]
    regularity = modal_n / len(gaps)
    if cv < 0.35 and len(gaps) >= 10 and regularity >= 0.6:
        tight = f"σ/μ={cv:.2f}, {regularity:.0%} of gaps in one interval class"
        sig.append(Signal("clock_like_cadence", "temporal", cv, min(1.0, (0.35 - cv) / 0.35 * regularity + 0.3), tight))
    burstiness = (sd_g - mean_g) / (sd_g + mean_g) if (sd_g + mean_g) else 0.0
    if burstiness > 0.55:
        sig.append(Signal("bursty_activity", "temporal", burstiness, min(1.0, (burstiness - 0.55) / 0.45),
                          f"inter-post gaps burstiness B={burstiness:.2f}"))

    # circadian flatness (weak signal alone, never gating)
    hours = [t.hour for t in ts]
    flat = _entropy([hours.count(h) for h in range(24)])
    if flat > 0.92 and len(posts) >= 30:
        sig.append(Signal("flat_24h_activity", "temporal", flat, 0.25,
                          "activity spread evenly over 24h with no sleep window"))

    # ---- content family ----
    sh = [shingles(p.text) for p in posts]
    dup_pairs, dup_examples = 0, []
    n = len(sh)
    sample = posts if n <= 60 else posts[:: max(1, n // 60)]
    idx = {id(p): i for i, p in enumerate(posts)}
    cmp_count = 0
    for i, p in enumerate(sample):
        for q in sample[i + 1:]:
            a, b = sh[idx[id(p)]], sh[idx[id(q)]]
            if not a or not b:
                continue
            cmp_count += 1
            inter = len(a & b)
            j = inter / (len(a) + len(b) - inter)
            if j >= 0.8:
                dup_pairs += 1
                if len(dup_examples) < 3:
                    dup_examples.append(p.post_id)
    dup_rate = dup_pairs / cmp_count if cmp_count else 0.0
    if dup_rate >= 0.15:
        sig.append(Signal("near_duplicate_content", "content", dup_rate, min(1.0, dup_rate / 0.5),
                          f"{dup_rate:.0%} of sampled post pairs are near-duplicates (Jaccard>=0.8)", ))
        res.evidence_post_ids.extend(dup_examples)

    # hashtag-sequence recycling
    tagseqs = Counter(tuple(sorted(p.hashtags)) for p in posts if p.hashtags)
    reused = sum(c for _, c in tagseqs.items() if c >= 4)
    if tagseqs and reused / max(1, sum(tagseqs.values())) >= 0.5:
        top = tagseqs.most_common(1)[0]
        sig.append(Signal("recycled_hashtag_sequences", "content", reused, 0.5,
                          f"tag sequence {list(top[0])[:5]} reused {top[1]}x"))

    # URL reuse
    doms = Counter(d.split("://")[-1].split("/")[0] for p in posts for d in p.urls)
    if doms:
        top_share = doms.most_common(1)[0][1] / sum(doms.values())
        if top_share >= 0.75 and sum(doms.values()) >= 5:
            sig.append(Signal("dominant_url_reuse", "content", top_share, 0.45,
                              f"{top_share:.0%} of shared URLs point to {doms.most_common(1)[0][0]}"))

    # ---- behavioral family ----
    repost_n = sum(1 for p in posts if p.repost_of)
    repost_ratio = repost_n / len(posts)
    if repost_ratio >= 0.8:
        sig.append(Signal("repost_dominant", "behavioral", repost_ratio, min(1.0, repost_ratio),
                          f"{repost_ratio:.0%} of activity are reposts"))
    # same-source repost storm: >5 reposts of posts by one author in <1h windows
    src_by_hour: Counter = Counter()
    for p in posts:
        if p.repost_of:
            src_by_hour[(p.repost_of.split(":", 1)[0], post_timestamp(p).strftime("%Y%m%d%H"))] += 1
    storm = max(src_by_hour.values(), default=0)
    if storm >= 5:
        sig.append(Signal("single_source_repost_storm", "behavioral", storm, 0.55,
                          f"up to {storm} reposts from one source author within single hours"))

    # ---- metadata family (weak) ----
    meta = account_meta or {}
    if meta.get("default_profile") is True:
        sig.append(Signal("default_profile", "metadata", 1.0, 0.15, "default avatar/banner at collection time"))

    # ---- v2 additions -------------------------------------------------------
    # templated messaging: message frames identical once URL/number/user slots vary.
    # Skeleton guard (>=5 fixed words) prevents short formulaic human text firing.
    def _skeleton(t: str) -> int:
        return sum(1 for w in t.split() if w not in _SLOT_TOKENS)

    tpls = Counter(_template(p.text) for p in posts if p.text and len(p.text) > 15)
    hot_tpl = [(k, c) for k, c in tpls.items() if c >= 4 and _skeleton(k) >= 6]
    if hot_tpl:
        share = sum(c for _, c in hot_tpl) / max(1, sum(tpls.values()))
        if share >= 0.4:
            sig.append(Signal("templated_messaging", "content", share, min(1.0, 0.5 + share / 2),
                              f"{share:.0%} of posts share variable-slot templates (URL/number/user normalized)"))

    # mention blast: one message frame fired at many distinct victim sets per hour
    by_win: dict[str, dict[str, list[set]]] = defaultdict(lambda: defaultdict(list))
    for p in posts:
        mens = p.mentions or _USER_RE.findall(p.text or "")
        if mens and len(mens) >= 2:
            w = post_timestamp(p).strftime("%Y%m%d%H")
            by_win[w][_template(p.text)].append(set(m.lower().lstrip("@") for m in mens))
    blast = 0
    for groups in by_win.values():
        for victims in groups.values():
            if len(victims) >= 3:
                u = set.union(*victims)
                i_ = set.intersection(*victims)
                if len(u - i_) >= 4:
                    blast = max(blast, len(victims))
    if blast >= 3:
        sig.append(Signal("mention_blast", "behavioral", blast, 0.6,
                          f"same message frame sent to {blast} distinct mention sets within one hour"))

    # metadata v2: fresh account + extreme follow ratio (weak, never gating)
    created = meta.get("created_at")
    if created:
        try:
            c_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            if c_at.tzinfo is None:
                c_at = c_at.replace(tzinfo=timezone.utc)
            t0 = ts[0] if ts[0].tzinfo else ts[0].replace(tzinfo=timezone.utc)
            age_days = (t0 - c_at).days
            if 0 <= age_days <= 14 and len(posts) >= 20:
                sig.append(Signal("fresh_account_burst", "metadata", age_days, 0.3,
                                  f"account created {age_days}d before observed activity"))
        except (ValueError, TypeError):
            pass
    fr, fg = meta.get("followers"), meta.get("following")
    if fr is not None and fg is not None and fg >= 1000 and fr <= 50:
        sig.append(Signal("extreme_follow_ratio", "metadata", float(fg), 0.25,
                          f"following {fg} vs {fr} followers at snapshot"))

    # Tier-2 content fusion (stylistic LLM-vs-human classifier output)
    cc = content_class or {}
    ccls = cc.get("classification")
    if ccls == "ai_generated_suspected":
        ai_med = float(cc.get("ai_score") or 50)
        share_flag = float(cc.get("share_ai_flagged") or 0.5)
        sig.append(Signal("llm_style_content", "content", ai_med,
                          min(0.8, 0.35 + 0.35 * min(1.0, share_flag)),
                          f"Tier-2 stylistic: median AI-likeness {ai_med}/100 over {cc.get('n')} texts"))
    elif ccls == "human_style":
        res.caveats.append("Tier-2 stylistic classifier: text reads human-written (counterevidence to automation)")

    # ---- composite with family gating ----
    if not sig:
        res.verdict = VERDICT_LIKELY_HUMAN
        res.automation_score = round(100 * min(0.35, len(posts) / 500), 1)  # volume only caps "low" score
        res.confidence = 0.4
        res.caveats.append("no automation signals above thresholds; high volume alone was NOT scored as automation")
        return res

    weights = {"temporal": 0.35, "content": 0.35, "behavioral": 0.2, "metadata": 0.1}
    fam: dict[str, float] = defaultdict(float)
    for s in sig:
        fam[s.family] = max(fam[s.family], s.score)
    score = sum(w * fam.get(f, 0.0) for f, w in weights.items())
    score = min(1.0, score / (sum(weights.values()))) * 100

    families_present = {s.family for s in sig} - {"metadata"}
    strong = [s for s in sig if s.score >= 0.4]
    strong_families = {s.family for s in strong} - {"metadata"}

    # v2: partial-automation (mixed) detection — one timeline half templated,
    # the other reads organic; escalation would be over-claiming.
    mixed = False
    if len(posts) >= 40:
        half = len(posts) // 2

        def _dr(chunk: list[PostObs]) -> float:
            s = [shingles(p.text) for p in chunk][:40]
            pairs = dup = 0
            for i in range(len(s)):
                for j in range(i + 1, len(s)):
                    a, b = s[i], s[j]
                    if not a or not b:
                        continue
                    pairs += 1
                    inter = len(a & b)
                    if inter and inter / (len(a) + len(b) - inter) >= 0.8:
                        dup += 1
            return dup / pairs if pairs else 0.0

        d1, d2 = _dr(posts[:half]), _dr(posts[half:])
        if abs(d1 - d2) >= 0.15 and max(d1, d2) >= 0.2:
            mixed = True

    if len(strong_families) >= 2 and score >= 45:
        if mixed and score < 62:
            res.verdict = VERDICT_MIXED_AUTOMATION
            res.caveats.append("automation pattern covers only part of the timeline (base activity reads human) — "
                               "automation-assist hypothesis, not full automation claim")
        else:
            res.verdict = VERDICT_LIKELY_AUTOMATED
        if ccls == "human_style" and score < 58:
            res.verdict = VERDICT_INDETERMINATE
            res.caveats.append("downgraded: Tier-2 stylistic evidence contradicts automation; analyst review advised")
    elif len(families_present) >= 2 and score >= 35:
        res.verdict = VERDICT_INDETERMINATE
        res.caveats.append("signals present but below coordination-strength gate; human operation not excluded")
    elif score < 35:
        res.verdict = VERDICT_LIKELY_HUMAN
    else:
        res.verdict = VERDICT_INDETERMINATE
        res.caveats.append("single-family signals only — deliberately not escalated to likely_automated")

    res.automation_score = round(score, 1)
    coverage = min(1.0, len(posts) / 100) * min(1.0, span_h / 48)
    res.confidence = round(0.3 + 0.7 * coverage * (0.6 + 0.4 * min(1.0, len(sig) / 4)), 2)
    res.signals = sig
    return res
