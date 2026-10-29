"""HERMES moderation-matrix integration tests: dual scoring, multiplier,
thresholds, normalization (tashkeel/tatweel/alef), Arabizi, fuzzy matching."""
from pickemup_analysis.risk import (ALLOW, BLOCK, FLAG, WARN, action_for,
                                     classify, matrix_scan, _anorm, _arabizi_to_ar)


def test_spec_example_violence_plus_group_blocks():
    r = classify("اقتلوا أحفاد القردة والخنازير لا تبقوا منهم")
    assert r.action == BLOCK
    assert r.threat_score >= 0.80
    assert "violent_incitement_commands" in r.matrix_categories
    assert "anti_jewish_anti_israel_rhetoric" in r.matrix_categories
    assert r.human_review_required is True
    m = r.to_dict()["moderation"]
    assert m["flagged"] is True and m["scores"]["extremism_threat_score"] >= 0.8


def test_group_terms_alone_never_block():
    # political/identity terms alone must stay below BLOCK band
    for t in ("الكيان الصهيوني", "يهود", "صهاينة", "روافض"):
        r = classify(t)
        assert r.threat_score <= 0.75, t
        assert r.action != BLOCK, t
    # dehumanizing slur without violence -> FLAG band at most
    r = classify("أحفاد القردة والخنازير")
    assert r.threat_score == 0.75 and r.action == FLAG


def test_profanity_independent_axis_not_terrorism():
    r = classify("كسمك يا حقير")
    assert r.profanity_score >= 0.80
    assert r.threat_score < 0.40
    assert r.action == WARN
    assert r.category == "harassment_vulgarity"
    assert "harassment" in r.to_dict()["moderation"]["reasoning"].lower()


def test_prof_plus_moderate_threat_blocks():
    assert action_for(0.60, 0.85) == BLOCK
    assert action_for(0.79, 0.30) == FLAG
    assert action_for(0.39, 0.69) == ALLOW


def test_normalization_tashkeel_tatweel_alef():
    r = classify("إقْتَلُـوهُم")  # tashkeel + tatweel + alef variants
    assert "violent_incitement_commands" in r.matrix_categories


def test_arabizi_digit_conversion():
    # real digit-Arabizi ("7aqer" = حقير, "kffar" ~ كفار) converts + fuzzy-matches;
    # plain English must never convert, even with digits present
    r = classify("kffar ya 7aqer")
    assert "general_sectarian_takfiri" in r.matrix_categories
    assert r.threat_score >= 0.40 and r.action == FLAG
    e = classify("the 7 camels crossed 20 dunes near kafar valley")
    assert e.action == "ACTION_ALLOW" and e.matrix_categories == []


def test_fuzzy_symbol_substitution():
    # replace letter with injected typo (distance 1)
    r = classify("اقت1لوا")
    assert r.action in (BLOCK, FLAG)
    assert "violent_incitement_commands" in r.matrix_categories


def test_countering_context_downgrades():
    r = classify("تقرير إخباري: المنشور قال اقتلوا ثم أضاف خطاب الكراهية مرفوض")
    assert r.action != BLOCK  # reporting context caps enforcement
    assert any("downgraded" in c or "capped" in c or "refut" in c.lower() for c in r.caveats)


def test_clean_text_allowed():
    r = classify("Great fresh bread and coffee near the Corniche this morning.")
    assert r.action == ALLOW and r.category == "benign" and not r.to_dict()["moderation"]["flagged"]


def test_moderation_schema_shape():
    m = classify("اقتلوا الكفار").to_dict()["moderation"]
    assert set(m) == {"flagged", "action", "scores", "detected_categories", "matched_terms", "reasoning"}
    assert set(m["scores"]) == {"extremism_threat_score", "profanity_harassment_score"}


# ---- false-positive tightening (operator-flagged mislabels, 2026-10-29) ----

def test_casual_filthy_is_not_hate_speech():
    r = classify("gg that was a filthy win, my aim was filthy today lol")
    assert r.action == ALLOW and r.category == "benign"


def test_reported_speech_downgraded_out_of_review_queue():
    r = classify("According to the prosecutor, the suspect shouted death to all of them in court")
    assert r.action in (ALLOW, WARN)


def test_directed_dehumanization_still_escalates():
    r = classify("they are filthy animals, subhuman vermin that must be cut out")
    assert r.action in (FLAG, BLOCK)


def test_directional_breed_slur_still_flags():
    r = classify("their breed must be erased from this land")
    assert r.action in (FLAG, BLOCK)
