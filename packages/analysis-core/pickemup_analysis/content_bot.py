"""Tier-2 content classifier: human-style vs LLM-generated post text.

Deterministic, explainable stylistic-feature scorer used as an
*unsupervised prior*; logistic weights were calibrated and measured against a
public labeled bot benchmark (see WEIGHTS provenance below). It never claims
intent or account verdict: output is per-text style likelihood only;
account-level fusion happens in automation.py.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field

from . import ALGO_VERSION
from .similarity import normalize

HUMAN_STYLE = "human_style"
AI_SUSPECTED = "ai_generated_suspected"
INDETERMINATE = "indeterminate"
INSUFFICIENT = "insufficient"

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2764]", re.UNICODE)
_RUN_RE = re.compile(r"(\w)\1{2,}")  # soooo, noooo — human emphasis typing
_CAPS_TOKEN_RE = re.compile(r"\b[A-Z]{3,}\b")  # ALL-CAPS shouting
_SENT_SPLIT_RE = re.compile(r"[.!?؟\.]+\s|\s[-—•]\s")

# Formulaic register that LLM output over-uses in short social text
_FORMAL_MARKERS = [
    "moreover", "furthermore", "in conclusion", "it's essential", "it is essential",
    "delve", "navigate the", "landscape of", "in today's", "it's important to note",
    "in the realm of", "a testament to", "plays a crucial role", "seamless", "elevate",
    "unlock the", "harness the", "ever-evolving", "multifaceted", "in summary",
]
def _marker_hits(tl: str, compiled) -> int:
    """Count markers with word boundaries for single words; phrase markers
    (containing a space, e.g. "in today's") stay plain substring matches."""
    return sum(1 for pat, word in compiled if (pat.search(tl) if word else pat in tl))


_HUMAN_MARKERS = [
    "idk", "tbh", "imo", "lol", "omg", "wtf", "y'all", "gonna", "wanna", "gotta",
    "kinda", "sorta", "nah", "yea", "ugh", "smh", "fr ", "ngl", "cuz", "tho",
]


def _compile(markers):
    out = []
    for m in markers:
        word = " " not in m.strip()
        out.append((re.compile(r"(?<!\w)" + re.escape(m.strip()) + r"(?!\w)") if word else m, word))
    return out


_HUMAN_HITS = _compile(_HUMAN_MARKERS)
_FORMAL_HITS = _compile(_FORMAL_MARKERS)



@dataclass
class FeatureContribution:
    name: str
    value: float
    contribution: float   # + pushes toward AI-style, - toward human-style
    detail: str


@dataclass
class ContentBotResult:
    classification: str
    ai_score: float                 # 0..100
    confidence: str                 # high|medium|low|insufficient
    features: list[FeatureContribution] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    algo_version: str = ALGO_VERSION

    def to_dict(self) -> dict:
        return {
            "classification": self.classification, "ai_score": round(self.ai_score, 1),
            "confidence": self.confidence,
            "features": [{"name": f.name, "value": round(f.value, 3),
                          "contribution": round(f.contribution, 3), "detail": f.detail}
                         for f in self.features],
            "caveats": self.caveats, "algo_version": self.algo_version,
        }


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# Calibrated logistic weights — fit on the TRAIN split of a public labeled
# bot benchmark (Zenodo record 8035290, CC-BY-4.0; Grammasch, Garla, Yang,
# Menczer); held-out test accuracy 0.804 / precision 0.745 / recall 0.926.
# z = bias + sum(w * (feature_mean - mean)/sd); P(bot) = sigmoid(z).
# IMPORTANT: precision 0.745 means ~1 in 4 flagged accounts is human — Tier-2
# output is evidence for fusion, never a standalone accusation.
# English-only calibration (the benchmark is an English dataset); non-English falls
# back to the unsupervised hand-set prior with capped confidence.
_CAL = {
    "bias": -0.244153,
    "caps_shouting": (0.02566, 0.0224, 0.0711),
    "emphasis_runs": (0.176963, 0.0405, 0.2369),
    "emoji_rate": (-0.205472, 0.0297, 0.156),
    "social_tokens": (0.200281, 0.635, 1.1375),
    "chat_slang": (-0.022097, 0.1409, 0.4005),
    "formal_llm_register": (0.406895, 0.0009, 0.0296),
    "punct_density": (0.703962, 0.2241, 0.1265),
    "mean_word_length": (0.592495, 4.3029, 0.7861),
    "contractions": (0.930985, 0.0198, 0.0361),
    "long_sentences": (1.906326, 0.7569, 0.5221),
}


def _sigmoid(x: float) -> float:
    return 100.0 / (1.0 + pow(2.718281828, -max(-30.0, min(30.0, x))))


def classify_text(text: str) -> ContentBotResult:
    t = unicodedata.normalize("NFKC", (text or "").strip())
    res = ContentBotResult(INDETERMINATE, 50.0, "insufficient")
    words = re.findall(r"[A-Za-z\u0600-\u06ff']+", t)
    n = len(words)
    if len(t) < 25 or n < 5:
        res.caveats.append("text too short for stylistic analysis")
        res.classification = INSUFFICIENT
        return res

    tl = t.lower()
    norm_words = normalize(tl).split()
    ftrs: list[FeatureContribution] = []

    def add(name: str, value: float, contribution: float, detail: str):
        ftrs.append(FeatureContribution(name, value, _clamp(contribution), detail))

    total_chars = max(1, len(t))

    # 1) Capitalized-shouting rate — humans do it, LLM prose rarely does
    caps_tokens = len(_CAPS_TOKEN_RE.findall(t))
    caps_rate = caps_tokens / n
    add("caps_shouting", caps_rate, -0.9 * min(1.0, caps_rate / 0.15),
        f"{caps_tokens}/{n} words fully capitalized")

    # 2) repeated-character emphasis runs (sooo, nooo)
    runs = len(_RUN_RE.findall(tl))
    add("emphasis_runs", runs, -0.8 * min(1.0, runs / 2), f"{runs} repeated-char emphasis runs")

    # 3) emoji rate
    emo = len(_EMOJI_RE.findall(t))
    add("emoji_rate", emo / n, -0.7 * min(1.0, emo / 2), f"{emo} emoji")

    # 4) hashtags/mentions — human social-typing habit
    tags = tl.count("#") + tl.count("@")
    add("social_tokens", tags, -0.6 * min(1.0, tags / 2), f"{tags} #/@ tokens")

    # 5) abbreviation/slang markers (word-boundary: "yea" must not match "year")
    hm = _marker_hits(tl, _HUMAN_HITS)
    add("chat_slang", hm, -1.0 * min(1.0, hm), f"{hm} chat-slang markers")

    # 6) formal/LLM-register markers
    fm = _marker_hits(tl, _FORMAL_HITS)
    add("formal_llm_register", fm, 1.0 * min(1.0, fm / 2), f"{fm} formulaic-register markers")

    # 7) punctuation density per word (structured prose vs loose typing)
    punct = sum(tl.count(p) for p in ".,;:") / max(1, n)
    add("punct_density", punct, 0.8 * _clamp((punct - 0.12) / 0.25),
        f"{punct:.2f} sentence/clause punctuation per word")

    # 8) mean word length (LLM vocabulary skews longer)
    awl = sum(len(w) for w in words) / n
    add("mean_word_length", awl, 0.7 * _clamp((awl - 4.3) / 1.7), f"avg {awl:.1f} chars/word")

    # 9) contraction usage (humans contract far more)
    contr = tl.count("'") + tl.count("’")
    add("contractions", contr / n, -0.7 * min(1.0, (contr / n) / 0.12),
        f"{contr} contractions/apostrophes")

    # 10) multi-clause sentence structure
    sents = [s for s in _SENT_SPLIT_RE.split(tl) if len(s.split()) > 0]
    long_sents = sum(1 for s in sents if len(s.split()) >= 9)
    add("long_sentences", long_sents, 0.6 * _clamp((long_sents - 1) / 3) if len(sents) > 1 else 0.0,
        f"{long_sents}/{len(sents)} sentences >=9 words")

    # 11) URL presence neutralizes formal-essay penalty lightly
    if "http" in tl:
        for f in ftrs:
            if f.name == "formal_llm_register" and f.contribution > 0:
                f.contribution *= 0.7

    # calibrated logistic (English) vs hand-set unsupervised prior (other
    # languages). The calibration loop OVERWRITES contributions, so the manual
    # ones are captured first and restored for the non-English path —
    # otherwise e.g. the non-English URL-neutralization would be silently lost.
    from .risk import detect_lang
    lang = detect_lang(t)
    calibrated = lang == "en"
    manual_sum = sum(f.contribution for f in ftrs)
    if calibrated:
        feat_map = {f.name: f for f in ftrs}
        z = _CAL["bias"]
        for name, spec in _CAL.items():
            if name == "bias":
                continue
            w, mu, sd = spec
            v = feat_map[name].value if name in feat_map else mu
            contrib = w * (v - mu) / sd
            if name in feat_map:
                feat_map[name].contribution = _clamp(contrib / 3.0)
            z += contrib
        ai = _sigmoid(z)
    else:
        ai = 100.0 / (1.0 + pow(2.718281828, -2.2 * manual_sum / max(3.0, len(ftrs) * 0.6)))
    res.features = sorted(ftrs, key=lambda f: -abs(f.contribution))
    res.ai_score = round(ai, 1)

    decisive = sum(1 for f in ftrs if abs(f.contribution) >= 0.3)
    if ai >= 62:
        res.classification = AI_SUSPECTED
    elif ai <= 40:
        res.classification = HUMAN_STYLE
    else:
        res.classification = INDETERMINATE
    base_conf = ("insufficient" if decisive < 3 else
                 "high" if abs(ai - 50) >= 28 and decisive >= 5 else
                 "medium" if abs(ai - 50) >= 15 else "low")
    if calibrated:
        res.caveats.append("tier-2 calibrated on a public EN bot benchmark: held-out accuracy 0.80, precision 0.75 — "
                           "~1 in 4 flagged is human; fusion + analyst review required")
    else:
        if base_conf == "high":
            base_conf = "medium"  # non-English: uncalibrated prior
        res.caveats.append("tier-2 non-English text: uncalibrated stylistic prior (calibration is English-only)")
    res.confidence = base_conf
    return res


def account_features(texts: list[str]) -> dict[str, float] | None:
    """Account-level feature means — the exact space the benchmark logistic was
    fit in. (Per-text scores saturate at 0/100; aggregating those loses the
    account evidence the model was calibrated on.)"""
    per = [classify_text(t) for t in texts]
    usable = [p for p in per if p.classification != INSUFFICIENT]
    if len(usable) < 5:
        return None
    names = [k for k in _CAL if k != "bias"]
    sums = {n: 0.0 for n in names}
    for p in usable:
        fmap = {f.name: f.value for f in p.features}
        for n in names:
            sums[n] += fmap.get(n, _CAL[n][1])
    return {n: sums[n] / len(usable) for n in names}


def score_account_features(feats: dict[str, float]) -> float:
    z = _CAL["bias"]
    for n, spec in _CAL.items():
        if n == "bias":
            continue
        w, mu, sd = spec
        z += w * (feats.get(n, mu) - mu) / sd
    return _sigmoid(z)


def classify_posts(texts: list[str]) -> dict:
    """Account classification. Majority-English accounts use the calibrated
    logistic over account-mean features (identical estimator to
    calibration/test on the public benchmark); accounts whose text is mostly non-English
    fall back to the language-aware per-text prior with demoted confidence —
    the calibration corpus is English-only."""
    from .risk import detect_lang
    per = [classify_text(t) for t in texts]
    usable = [p for p in per if p.classification != INSUFFICIENT]
    if len(usable) < 5:
        return {"classification": INSUFFICIENT, "ai_score": 50.0, "n": len(usable),
                "confidence": "insufficient", "caveats": ["fewer than 5 analyzable texts"]}
    share = sum(1 for p in usable if p.ai_score >= 62) / len(usable)
    en_share = sum(1 for x in texts if detect_lang(x) == "en") / max(1, len(texts))
    if en_share < 0.5:
        usable_scores = sorted(p.ai_score for p in usable)
        fused = usable_scores[len(usable_scores) // 2]  # median per-text prior
        pack = _account_pack(fused, len(usable), share_ai=share, calibrated=False)
        pack["confidence"] = "low"
        pack["caveats"].append(
            f"account text is {round((1 - en_share) * 100)}% non-English: language-aware "
            "per-text prior used (account-level calibration is English-only)")
        return pack
    names = [k for k in _CAL if k != "bias"]
    sums = {n: 0.0 for n in names}
    for p in usable:
        fmap = {f.name: f.value for f in p.features}
        for n in names:
            sums[n] += fmap.get(n, _CAL[n][1])
    fused = score_account_features({n: sums[n] / len(usable) for n in names})
    return _account_pack(fused, len(usable), share_ai=share)


def _account_pack(fused: float, n: int, share_ai: float | None = None,
                  calibrated: bool = True) -> dict:
    if fused >= 52:
        cls = AI_SUSPECTED
    elif fused <= 48:
        cls = HUMAN_STYLE
    else:
        cls = INDETERMINATE
    conf = "medium" if n >= 15 else "low"
    out = {"classification": cls, "ai_score": round(fused, 1), "n": n,
           "confidence": conf if cls != INDETERMINATE else "low",
           "caveats": ([
               "account score = calibrated logistic over account-mean features (EN); "
               "held-out benchmark accuracy 0.80 / precision 0.75 / recall 0.93 — "
               "~1 in 4 flagged is human; never a standalone accusation"]
               if calibrated else [
               "account score = language-aware per-text stylistic prior (uncalibrated); "
               "analytical support only, never a standalone accusation"])}
    if share_ai is not None:
        out["share_ai_flagged"] = round(share_ai, 2)
    return out
