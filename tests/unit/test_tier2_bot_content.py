"""Tests for Tier-2 content classifier, bilingual risk classifier,
and automation v2 signals (templating, mention blast, fusion, mixed tier)."""
from datetime import datetime, timedelta

from pickemup_analysis.automation import score_account
from pickemup_analysis.content_bot import AI_SUSPECTED, HUMAN_STYLE, INSUFFICIENT, classify_posts, classify_text
from pickemup_analysis.domain import PostObs
from pickemup_analysis.risk import (HIGH, MODERATE, NONE_, classify)


def _p(i, text, t0, **kw):
    kw.setdefault("author_id", "a1")
    return PostObs(post_id=f"p{i}",
                   text=text, created_at=t0 + timedelta(minutes=i * 5), **kw)


# --------------------------- risk: bilingual ------------------------------
def test_risk_en_incitement_high_and_reviewable():
    r = classify("they deserve to die, hunt them down at the square tonight")
    assert r.category == "incitement_violence"
    assert r.severity == HIGH
    assert r.human_review_required
    assert r.matched_terms and r.algo_version


def test_risk_reporting_context_caps_severity():
    r = classify("BREAKING: according to news reports, the suspect shouted "
                 "'they deserve to die' — screenshot of the clip")
    assert r.severity != HIGH
    assert any("context markers" in c for c in r.caveats)


def test_risk_arabic_hate_and_counterspeech():
    h = classify("هؤلاء حقراء قذرون طائفتهم الملعونة تفسد كل شيء")
    assert h.category == "hate_speech" and h.severity in (HIGH, MODERATE)
    c = classify("لا للعنف لا للطائفية نرفض الكراهية تضامنا مع الضحايا")
    assert c.category == "counterspeech"
    assert c.severity == NONE_


def test_risk_arabizi_transliterates():
    r = classify("la ba3d h2ul al7u9ara2 anfi2at al7ay")  # arabizi hate-ish text
    assert r.language == "ar-latin"
    assert any("arabizi" in c for c in r.caveats)


def test_risk_benign_and_media_only():
    assert classify("beautiful morning coffee and a walk by the river").severity == NONE_
    m = classify("", has_media_only=True)
    assert m.confidence == "insufficient"


# --------------------------- tier-2 stylistic ------------------------------
LLMISH = ("Moreover, in today's ever-evolving landscape, it is essential to note "
          "that civic participation plays a crucial role in the realm of public discourse. "
          "In conclusion, we must delve into these multifaceted challenges together.")
HUMANISH = "idk tbh that ref totally robbed us lol wtf is even the call anymore #ballislife"


def test_content_tier_separates_styles():
    a = classify_text(LLMISH)
    h = classify_text(HUMANISH)
    assert a.classification == AI_SUSPECTED, a.__dict__
    assert h.classification == HUMAN_STYLE, h.__dict__
    assert a.ai_score > h.ai_score


def test_content_insufficient_for_short():
    assert classify_text("great day").classification == INSUFFICIENT


def test_content_posts_aggregation():
    agg = classify_posts([LLMISH] * 6 + ["hm ok"] * 6)
    assert agg["classification"] == AI_SUSPECTED and agg["n"] >= 6
    agg2 = classify_posts([HUMANISH] * 8)
    assert agg2["classification"] == HUMAN_STYLE


# --------------------------- automation v2 ---------------------------------
T0 = datetime(2026, 10, 20, 8, 0, 0)


def test_templated_messaging_signal():
    posts = [_p(i, f"Check out this expose on minister {i} at https://example{i % 2}.com/story{i}", T0)
             for i in range(12)]
    for j, q in enumerate(posts):
        q.created_at = T0 + timedelta(minutes=j * 15)
    res = score_account("a1", posts)
    assert any(s.name == "templated_messaging" for s in res.signals), [s.name for s in res.signals]


def test_mention_blast_signal():
    posts = [_p(i, f"@user{i} @acct{i} your scam page must be taken down today", T0)
             for i in range(5)]
    # 3 blasts inside hour 1, rest later so span clears 2h but blast window is one hour
    for j, q in enumerate(posts):
        q.created_at = T0 + timedelta(minutes=[0, 8, 19, 160, 175][j])
    res = score_account("a1", posts)
    assert any(s.name == "mention_blast" for s in res.signals), [s.name for s in res.signals]


def test_llm_style_fusion_signal():
    posts = [_p(i, f"Join the movement now! share wave {i // 4} https://example.com/w{i}", T0)
             for i in range(14)]
    for j, q in enumerate(posts):
        q.created_at = T0 + timedelta(minutes=j * 12)
    cc = {"classification": "ai_generated_suspected", "ai_score": 71.0, "n": 14, "share_ai_flagged": 0.8}
    res = score_account("a1", posts, content_class=cc)
    assert any(s.name == "llm_style_content" for s in res.signals)


def test_mixed_automation_not_overclaimed():
    # first half organic human chatter, second half repost-storm automation
    organic = [_p(i, f"sunny walk today thought {i} about dinner maybe {['pasta','soup','figs'][i%3]}", T0)
               for i in range(25)]
    auto = [_p(100 + i, "Same frame amplify now! https://example.com/x", T0,
               repost_of=f"src:{i % 2}") for i in range(25)]
    # make second half clock-like
    for j, q in enumerate(auto):
        q.created_at = T0 + timedelta(hours=4, minutes=j * 2)
    res = score_account("a1", organic + auto)
    assert res.verdict in ("mixed_automation", "likely_automated", "indeterminate")
    if res.verdict == "mixed_automation":
        assert any("part of the timeline" in c for c in res.caveats)


def test_human_style_counterevidence_caveat():
    posts = [_p(i, f"random human thought {i} about {(['weather','tea','rain','sun','wind','fog','snow','ice','hail','dew','mist','smog'])[i]} today", T0)
             for i in range(12)]
    for j, q in enumerate(posts):
        q.created_at = T0 + timedelta(minutes=j * 20)  # 4h spread
    cc = {"classification": "human_style", "ai_score": 22.0, "n": 12}
    res = score_account("a1", posts, content_class=cc)
    assert any("human-written" in c for c in res.caveats), res.caveats
