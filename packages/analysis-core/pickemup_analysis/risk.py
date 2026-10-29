"""Bilingual (Arabic + English) content-risk classifier — MVP.

Honest scope: this is a deterministic lexicon + TF-IDF-centroid classifier
(no torch/transformers wheels exist for this Python yet — a fine-tuned
mBERT/XLM-R sits behind the same interface as a future adapter).
Every output carries category, severity, contributing evidence, caveats,
and ALGO_VERSION. High-severity violence-related verdicts are ALWAYS
flagged for human review; the engine never claims authorship intent.

Context safeguards (directive §11): reporting/quotation/counterspeech
markers cap severity and add caveats. Content severity is kept separate
from identity, affiliation, or intent claims.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field

from . import ALGO_VERSION
from .similarity import normalize

CATEGORIES = (
    "extremist_propaganda", "hate_speech", "incitement_violence",
    "racism", "terrorist_support", "harassment_vulgarity", "counterspeech", "benign",
)
HIGH, MODERATE, NONE_ = "high", "moderate", "none"

_AR_RE = re.compile(r"[\u0600-\u06ff]")
_LAT_RE = re.compile(r"[a-z]")


def detect_lang(text: str) -> str:
    ar = len(_AR_RE.findall(text))
    la = len(_LAT_RE.findall(text.lower()))
    if ar > la * 0.4:
        return "ar"
    # Arabizi heuristic: latin text where digits substitute Arabic letters
    if re.search(r"\d(?:an|en|in|3|7).*\d|[akm]s?2|3ibn|7aqq|ktb?y|h5|w7d", text.lower()):
        return "ar-latin"
    return "en"


# --- Lexicons (clinical, maintained as engineering vocabulary — the
#     product's detection terms, not propaganda). Dialect coverage:
#     MSA + Levantine + Gulf + Egyptian; arabizi handled via translit map.
_LEX: dict[str, dict[str, float]] = {
    "incitement_violence": {
        "ar": {"اقتل": 1.0, "اقتلوهم": 1.0, "إقتل": 1.0, "هاجم": 0.9, "هاجمو": 0.9,
               "اضرب": 0.6, "حرق": 0.7, "احرقو": 0.8, "دمهم": 0.9, "طردوهم من": 0.5,
               "نلحقهم": 0.7, "نحرق": 0.8, "صفي": 0.6, "صفّو": 0.6, "دكّ": 0.8, "دكوا": 0.8},
        "en": {"kill them": 1.0, "kill all": 1.0, "attack them": 0.9, "attack the": 0.7, "storm the": 0.6,
               "burn their": 0.8, "spill their blood": 0.9, "hunt them down": 0.9, "no mercy": 0.7,
               "take up arms against": 0.8, "they deserve to die": 1.0, "purge the": 0.8},
    },
    "extremist_propaganda": {
        "ar": {"دولة باقية وتتمدد": 1.0, "باقية وتتمدد": 1.0, "الخلافة": 0.7, "راية التوحيد نحو": 0.6,
               "غزوة": 0.7, "الصحوة الجهادية": 0.8, "أرض الخلافة": 0.8, "فلا تهنوا": 0.5, "المدينة الفاضلة": 0.4},
        "en": {"caliphate": 0.7, "bayat": 0.6, "martyrdom operation": 0.9, "glorified attack": 0.8,
               "the faithful advance": 0.6, "righteous conquest": 0.7},
    },
    "terrorist_support": {
        "ar": ["فدا للبيعة", "أبطال الدولة", "عرس الطاغية", "تحرير السبايا", "مباركة العملية",
               "عملية بطولية", "أسود الدولة", "جزء من دولتنا"],
        "en": ["glorious attack", "praised the attack", "martyr's honor", "brave fighters of",
               "we support the attack", "heroic operation", "praise be to the", "they struck for us"],
    },
    "hate_speech": {
        "ar": {"قذر": 0.7, "قذرين": 0.7, "نفايات": 0.7,
               "حيوانات": 0.6, "فئران": 0.6, "أولاد الحرام": 0.6, "طائفتهم الملعونة": 0.9,
               "دمهم حلال": 1.0, "لا إنسانيين": 0.7, "أوباء": 0.6},
        "en": ["vermin", "subhuman", "infidels all", "they are animals",
               "filthy animals", "filthy infidels", "filthy vermin", "filthy creatures",
               "your breed", "their breed", "lesser breed",
               "go back to your country you", "cancer that must be cut", "not human", "cockroach"],
    },
    "racism": {
        "ar": {"عنصري": 0.7, "عنصرية": 0.7, "سلالة أدنى": 0.9, "أنقى": 0.6, "دمهم النجس": 0.9,
               "أبناء الأرض الطاهرة": 0.6, "العبيد": 0.5},
        "en": ["race traitor", "superior race", "less evolved", "stay in your lanes people",
               "breed out", "whiter", "great replacement", "mongrel"],
    },
    "counterspeech": {
        "ar": {"ضد": 0.4, "نرفض": 0.5, "لا للعنف": 0.9, "لا للطائفية": 0.9, "تضامنا مع": 0.5,
               "أدانة": 0.5, "ندين": 0.6, "رفض": 0.5, "سلميا": 0.3},
        "en": ["we condemn", "against hatred", "stand against", "counter speech", "hate is wrong",
               "solidarity with the victims", "rejecting racism", "report this post", "counter-speech"],
    },
}

# Reporting / quotation / academic context markers → severity cap + caveat
_CONTEXT_MARKERS = {
    "ar": ["نبأ", "تقرير", "وقال", "نقلاً", "نقلًا", "دراسة", "بحث", "أفادت", "وثق", "شاهد: ", "لقطة شاشة", "سخرية", "تهكم", "ما رأيكم"],
    "en": ["according to", "reportedly", "news", "study", "research", "screenshot of", "quoted",
           "breaking:", "journalist", "documented", "satire", "satirical", "ironic", "academic"],
}

_ARABIZI_DIGITS = str.maketrans({"3": "ع", "7": "ح", "5": "خ", "2": "ء", "6": "ه", "1": "١", "9": "ق"})

# --- Arabic typographic normalization (spec: HERMES moderation pipeline) ---
_TASHKEEL_RE = re.compile(r"[\u064B-\u0652\u0670\u0640]")  # diacritics + tatweel
_AR_NORM = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي"})


def _anorm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = _TASHKEEL_RE.sub("", s)
    return s.translate(_AR_NORM)


# Digraph-aware Arabizi → Arabic (longest-first replacement on latin runs)
_ARABIZI_LETTERS = [
    ("sh", "ش"), ("kh", "خ"), ("dh", "ذ"), ("gh", "غ"), ("th", "ث"), ("ch", "چ"),
    ("aa", "ا"), ("ee", "ي"), ("oo", "و"), ("ay", "اي"),
    ("3", "ع"), ("7", "ح"), ("5", "خ"), ("6", "ه"), ("9", "ق"), ("2", "ء"), ("1", "ئ"), ("4", "ت"),
    # plain transliteration letters (heuristic; applied after digraphs/digits)
    ("a", "ا"), ("b", "ب"), ("d", "د"), ("e", "ي"), ("f", "ف"), ("g", "غ"), ("h", "ه"),
    ("i", "ي"), ("j", "ج"), ("k", "ك"), ("l", "ل"), ("m", "م"), ("n", "ن"), ("o", "و"),
    ("q", "ق"), ("r", "ر"), ("s", "س"), ("t", "ت"), ("u", "و"), ("w", "و"), ("y", "ي"),
    ("z", "ز"), ("x", "خ"), ("v", "ف"), ("p", "ب"), ("c", "ك"),
]
_LATIN_RUN = re.compile(r"[a-z0-9]+")


def _arabizi_to_ar(s: str) -> str:
    def conv(run: str) -> str:
        out, i = [], 0
        while i < len(run):
            for a, b in _ARABIZI_LETTERS:
                if run.startswith(a, i):
                    out.append(b)
                    i += len(a)
                    break
            else:
                out.append(run[i])
                i += 1
        return "".join(out)

    return _anorm(_LATIN_RUN.sub(lambda m: conv(m.group(0)), s.lower()))


def _lev(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 1:
        return 2
    if a == b:
        return 0
    # bounded Levenshtein (distance <=1 fast path)
    la, lb = len(a), len(b)
    if la == lb:
        return 1 if sum(1 for x, y in zip(a, b) if x != y) == 1 else 2
    if lb == la + 1:
        a, b, la, lb = b, a, lb, la
    if la != lb + 1:
        return 2
    i = j = diff = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            diff += 1
            if diff > 1:
                return 2
            i += 1
    return 1


# --- HERMES moderation matrix (severity-weighted; ingested 2026-10-24) -------
MATRIX: dict[str, dict[str, float]] = {
    "anti_jewish_anti_israel_rhetoric": {
        "أحفاد القردة والخنازير": 0.95, "خنازير": 0.70, "قردة": 0.70, "أعداء الله": 0.75,
        "بنو قينقاع": 0.50, "بنو قريظة": 0.50, "الكيان الصهيوني": 0.40, "الكيان الغاصب": 0.50,
        "صهاينة": 0.30, "المستوطنين": 0.30, "يهود": 0.20, "صهيون": 0.20,
    },
    "anti_christian_rhetoric": {
        "عباد الصليب": 0.85, "عباد عيسى": 0.75, "صليبيين": 0.60, "كفار أصل": 0.70,
        "اهل الذمة": 0.30, "النصاري": 0.30,
    },
    "general_sectarian_takfiri": {
        "كفار": 0.50, "كافر": 0.40, "تكفير": 0.60, "مرتدين": 0.65, "روافض": 0.80,
        "مجوس": 0.75, "نواصب": 0.75, "المشركين": 0.45, "أعداء الدين": 0.70,
        "طواغيت": 0.70, "حاكمية": 0.50,
    },
    "violent_incitement_commands": {
        "اقتلوا": 1.00, "إقتل": 1.00, "اقتلهم": 1.00, "اذبحوا": 1.00, "إذبح": 1.00,
        "دمروا": 0.90, "طهروهم": 0.90, "إبادة": 0.95, "لا تبقوا منهم": 0.95,
        "احرقوا": 0.85, "دكوهم": 0.85, "هاجموا": 0.80, "نلاحقهم": 0.70,
    },
    "explicit_vulgarity": {
        "كوس أمك": 0.85, "كسمك": 0.85, "كسم": 0.80, "شرموطة": 0.85, "شرموط": 0.80,
        "منيوك": 0.80, "عرص": 0.75, "قحبة": 0.85, "زب": 0.70, "طيز": 0.65,
    },
    "general_insults": {
        "ابن الكلب": 0.50, "يا ابن الشرموطة": 0.85, "كلب": 0.30, "حمار": 0.25,
        "يا واطي": 0.40, "حقير": 0.35, "تفه": 0.30, "خرا": 0.45,
    },
}
GROUP_CATS = ("anti_jewish_anti_israel_rhetoric", "anti_christian_rhetoric", "general_sectarian_takfiri")
VIOLENT_CAT = "violent_incitement_commands"
CRUDE_CATS = ("explicit_vulgarity", "general_insults")

BLOCK, FLAG, WARN, ALLOW = ("ACTION_BLOCK", "ACTION_FLAG_HUMAN_REVIEW",
                            "ACTION_WARN_USER", "ACTION_ALLOW")

WATCHLIST_CAT = "watchlist"  # user-supplied trigger words (kept separate from curated MATRIX)


def matrix_scan(text: str, extra: dict[str, tuple[str, float]] | None = None
                ) -> tuple[float, float, list[str], list[str]]:
    """Returns (threat, profanity, categories, matched_terms) per the moderation
    spec: violence co-occurring with a targeted-group frame gets a 1.5x
    multiplier; group terms alone are capped below BLOCK level; crude language
    is scored on an independent axis.

    `extra` extends the match set at call time with USER-MAINTAINED trigger
    words: {term: (category, weight)}. They are matched by exactly the same
    normalisation, Arabizi transliteration, phrase and fuzzy rules as the
    curated matrix, so an analyst can teach the engine new vocabulary without
    touching code. Watchlist terms never enter the violence/group multiplier —
    their weight maps directly onto the threat axis."""
    # Transliteration applies only to genuine Arabic-script or digit-Arabizi
    # input (a word mixing letters and digits, e.g. "7aqer"): plain English can
    # never leak into Arabic-term matching. Academic Latin transliteration
    # without digits is a documented heuristic gap (orthography not recoverable).
    has_arabic = any("\u0600" <= ch <= "\u06FF" for ch in text)
    digit_arabizi = bool(re.search(r"[A-Za-z]*\d[A-Za-z]+|[A-Za-z]+\d[A-Za-z]*\d", text))
    forms = {_anorm(text.lower())}
    if re.search(r"[a-z0-9]", text.lower()) and (has_arabic or digit_arabizi):
        forms.add(_arabizi_to_ar(text))

    single: dict[str, list[tuple[str, str, float]]] = {}
    multi: list[tuple[str, str, str, float]] = []
    term_srcs: list[tuple[str, dict[str, float]]] = list(MATRIX.items())
    for term, (cat, w) in (extra or {}).items():
        term_srcs.append((cat, {term: float(w)}))
    for cat, terms in term_srcs:
        for term, w in terms.items():
            tn = _anorm(term.lower())  # text side is lowered; terms must be too
            if " " in tn:
                multi.append((term, tn.replace(" ", ""), cat, w))
            else:
                single.setdefault(tn, []).append((term, cat, w))

    hits: dict[str, tuple[str, float]] = {}
    for form in forms:
        f = _anorm(form)
        f_sq = f.replace(" ", "")
        consumed: set[str] = set()
        for t in (x for x in re.split(r"\s+", f) if x):
            if len(t) < 3 and t not in single:
                continue
            for orig, c, w in single.get(t, []):          # exact token match
                hits.setdefault(orig, (c, w))
                consumed.add(t)
        for t in (x for x in re.split(r"\s+", f) if x):   # fuzzy: typos/symbol swaps
            if t in consumed or len(t) < 4:
                continue
            for tn, lst in single.items():
                # FP guard (one edit on very short native words collides with
                # common vocabulary after folding): the TEXT side must be >=5
                # chars; the term side may stay 4 so genuine variant forms —
                # e.g. transliterated "كففار" vs term "كفار" — still match
                if len(t) >= 5 and len(tn) >= 4 and _lev(t, tn) <= 1:
                    for orig, c, w in lst:
                        hits.setdefault(orig, (c, w))
                    consumed.add(t)
                    break
        for orig, tnsq, c, w in multi:                    # phrases (space-insensitive)
            if tnsq in f_sq:
                hits.setdefault(orig, (c, w))

    violence_w = max((w for _, (c, w) in hits.items() if c == VIOLENT_CAT), default=0.0)
    group_w = max((w for _, (c, w) in hits.items() if c in GROUP_CATS), default=0.0)
    crude_w = max((w for _, (c, w) in hits.items() if c in CRUDE_CATS), default=0.0)
    watch_w = max((w for _, (c, w) in hits.items() if c == WATCHLIST_CAT), default=0.0)
    if violence_w and group_w:
        threat = min(1.0, 1.5 * violence_w)  # targeted violent command
    elif violence_w:
        threat = violence_w
    else:
        threat = min(0.75, group_w)  # group terms alone must not reach BLOCK band
    threat = max(threat, watch_w)  # analyst watchlist weight, no multiplier
    prof = crude_w
    cats = sorted({c for c, _ in hits.values()})
    return threat, prof, cats, sorted(hits)


def action_for(threat: float, prof: float) -> str:
    if threat >= 0.80 or (threat >= 0.60 and prof >= 0.80):
        return BLOCK
    if 0.40 <= threat < 0.80:
        return FLAG
    if prof >= 0.70 and threat < 0.40:
        return WARN
    return ALLOW


@dataclass
class RiskResult:
    category: str
    severity: str
    score: float
    language: str
    confidence: str
    matched_terms: list[str]
    caveats: list[str]
    human_review_required: bool
    algo_version: str = ALGO_VERSION
    # moderation-matrix fields (HERMES lexicon spec)
    threat_score: float = 0.0
    profanity_score: float = 0.0
    action: str = "ACTION_ALLOW"
    matrix_categories: list = field(default_factory=list)
    matched_matrix_terms: list = field(default_factory=list)
    reasoning: str = ""

    def to_dict(self) -> dict:
        return {
            "category": self.category, "severity": self.severity, "score": round(self.score, 1),
            "language": self.language, "confidence": self.confidence,
            "matched_terms": self.matched_terms[:8], "caveats": self.caveats,
            "human_review_required": self.human_review_required, "algo_version": self.algo_version,
            "moderation": {
                "flagged": self.action != "ACTION_ALLOW",
                "action": self.action,
                "scores": {"extremism_threat_score": round(self.threat_score, 3),
                           "profanity_harassment_score": round(self.profanity_score, 3)},
                "detected_categories": self.matrix_categories,
                "matched_terms": self.matched_matrix_terms[:10],
                "reasoning": self.reasoning,
            },
        }


# Flatten category → {term: weight} per language (list-form categories get 0.85)
def _flat_lex(lang_key: str) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for cat, v in _LEX.items():
        vals = v.get(lang_key, [])
        result[cat] = {t: 0.85 for t in vals} if isinstance(vals, list) else {k: w for k, w in vals.items() if w > 0}
    return result


LEX_AR = _flat_lex("ar")
LEX_EN = _flat_lex("en")


def classify(text: str, has_media_only: bool = False,
             custom: dict[str, tuple[str, float]] | None = None) -> RiskResult:
    raw = text or ""
    lang = detect_lang(raw)
    caveats: list[str] = []
    if has_media_only:
        return RiskResult("benign", NONE_, 0, lang, "insufficient", [],
                          ["media-only post: text analysis not possible"], False)
    norm = _anorm(normalize(raw))  # fold tashkeel/hamza so lexicon + centroid see one shape
    if lang == "ar-latin":
        norm = _anorm(normalize(raw.translate(_ARABIZI_DIGITS).lower()))
        caveats.append("arabizi transliterated before matching")
    ctx_markers = [m for m in (_CONTEXT_MARKERS["ar"] + _CONTEXT_MARKERS["en"]) if m in raw.lower() or m in norm]
    if ctx_markers:
        caveats.append(f"context markers present ({', '.join(ctx_markers[:3])}) — may be reporting/quotation")

    lex = LEX_AR if lang.startswith("ar") else LEX_EN
    scores: dict[str, float] = {}
    hits: dict[str, list[str]] = {}
    for cat, terms in lex.items():
        if cat == "benign":
            continue
        got = [(t, w) for t, w in terms.items() if _anorm(t) in norm or t in raw.lower()]
        if got:
            hits[cat] = [f"{t}" for t, _ in got]
            # diminishing returns on multiple hits of same family
            weights = sorted((w for _, w in got), reverse=True)
            score = 100 * (1 - math.prod([max(0.0, 1 - 0.75 * w) for w in weights]))
            scores[cat] = score
    # CS-style centroid similarity for the two content families with exemplars
    sim = _centroid_sim(norm, lang)
    for cat, s in sim.items():
        scores[cat] = max(scores.get(cat, 0), s)

    # moderation matrix pass (independent threat/profanity axes); `custom`
    # injects the operator's live watchlist terms (same matching pipeline)
    m_threat, m_prof, m_cats, m_terms = matrix_scan(raw, extra=custom)

    def _mk(category: str, severity: str, score: float, confidence: str,
            matched: list[str], human_review: bool) -> RiskResult:
        threat_final = max(m_threat, score / 100.0 if severity != NONE_ else 0.0)
        action = action_for(threat_final, m_prof)
        if ctx_markers and action == BLOCK:
            action = FLAG
            caveats.append("action downgraded BLOCK→FLAG: possible reporting/quotation context")
        if ctx_markers and action == FLAG and threat_final < 0.75:
            # quoted speech / news coverage of an incident is not the operator's
            # review queue unless a strong threat axis stands on its own
            action = WARN
            caveats.append("action downgraded FLAG→WARN: reporting/quotation context without a strong threat axis")
        if category == "counterspeech" and action == BLOCK:
            action = FLAG
            caveats.append("counterspeech framing: capped at FLAG per directive §11 — "
                           "violent vocabulary may be quoted/opposed, not endorsed")
        if action == BLOCK:
            severity = HIGH
            if category == "benign":  # matrix-only match: category must not stay benign
                if WATCHLIST_CAT in m_cats and m_threat >= m_prof:
                    category = "watchlist_match"
                else:
                    category = "incitement_violence" if VIOLENT_CAT in m_cats else "hate_speech"
        elif action in (FLAG, WARN) and severity == NONE_:
            severity = MODERATE
            if category == "benign":
                if WATCHLIST_CAT in m_cats and m_threat >= m_prof:
                    category = "watchlist_match"
                else:
                    category = "harassment_vulgarity" if m_prof >= m_threat else "hate_speech"
        elif action == ALLOW and severity != NONE_ and score < 30:
            severity = NONE_
            category = "benign"
        reasoning = _reasoning(m_cats, threat_final, m_prof)
        return RiskResult(category, severity, score, lang, confidence, matched, caveats,
                          human_review or action in (FLAG, BLOCK),
                          threat_score=threat_final, profanity_score=m_prof, action=action,
                          matrix_categories=m_cats, matched_matrix_terms=m_terms,
                          reasoning=reasoning)

    if not scores:
        if not m_cats:
            return RiskResult("benign", NONE_, 0, lang,
                              "low" if len(norm) < 15 else "high", [], caveats, False,
                              profanity_score=0.0, reasoning="No matrix categories matched.")
        return _mk("benign", NONE_, round(100 * max(m_threat, m_prof), 1), "medium", [], False)

    category = max(scores, key=lambda k: scores[k])
    score = scores[category]
    cs = scores.get("counterspeech", 0)
    if category != "counterspeech" and cs >= score:
        category = "counterspeech"
        score = cs
    if category == "counterspeech":
        severity = NONE_
    elif score >= 60:
        severity = HIGH
    elif score >= 30:
        severity = MODERATE
    else:
        severity = NONE_
        category = "benign" if not ctx_markers else category

    if ctx_markers and severity == HIGH:
        severity = MODERATE
        caveats.append("severity capped: possible reporting/quotation context — analyst review advised")

    confidence = "high" if score >= 75 else "medium" if score >= 45 else "low"
    if score >= 75:
        caveats.append("lexicon-match classification; treat as triage signal, not proof of intent")
    human_review = severity == HIGH and category in {
        "incitement_violence", "terrorist_support", "extremist_propaganda"}
    return _mk(category, severity, score, confidence, hits.get(category, []), human_review)


def _reasoning(m_cats: list[str], threat: float, prof: float) -> str:
    violent = VIOLENT_CAT in m_cats
    group = any(c in GROUP_CATS for c in m_cats)
    crude = any(c in CRUDE_CATS for c in m_cats)
    if violent and group:
        return ("Direct violent command combined with targeted-group rhetoric "
                "(1.5x co-occurrence multiplier applied).")
    if violent:
        return "Violent command language present without an explicit targeted group."
    if group:
        return ("Targeted-group rhetoric without violent intent — capped below BLOCK band "
                "per policy (group terms alone are not treated as terrorism).")
    if crude:
        return ("Crude/insulting language on the harassment axis; scored independently of "
                "threat to avoid misclassifying interpersonal arguments as extremism.")
    if threat or prof:
        return "Moderate signal on one axis; no category escalation."
    return "No matrix categories matched."


# --- small TF-IDF-centroid layer over curated exemplars (no external deps) --
_EXEMPLARS: dict[str, dict[str, list[str]]] = {
    "ar": {
        "hate_speech": [
            "هؤلاء القذرون يجب أن يختفوا من شوارعنا النظيفة",
            "أنفاسهم نجسة طائفتهم الملعونة تفسد كل شيء",
            "حشرات لا بشرا يسكنون حيّنا يجب اقتلاعهم",
        ],
        "racism": [
            "سلالة أدنى لا تفهم إلا لغة القوة والترحيل",
            "دم النجس مختلط فيهم لا أصول ولا كرامة",
        ],
        "incitement_violence": [
            "اقتلوهم حيث ثقفتموهم ولا ترحموا أحدا اليوم",
            "نلحق بهم الليلة ونحرق بيوتهم فوق رؤوسهم",
        ],
        "extremist_propaganda": [
            "الدولة باقية وتتمدد بإذن الله فبايعوا ولا تتوانوا",
            "الخلافة قادمة رايتنا فوق كل الحدود",
        ],
        "terrorist_support": [
            "مباركة العملية البطولية أسود الدولة أكرمونا",
            "فدا للبيعة أبطال غزوة الأمس عرس للطاغية",
        ],
        "counterspeech": [
            "نرفض العنف الطائفي قلبا وقالبا تضامنا مع الضحايا",
            "لا للعنصرية لا للكراهية شعب واحد كريم",
        ],
    },
    "en": {
        "hate_speech": [
            "they are vermin crawling through our clean streets exterminate the breed",
            "subhuman filth every one of them should vanish from our city",
        ],
        "racism": [
            "a less evolved race must stay in their lanes before the replacement finishes us",
            "superior bloodlines only the mongrels breed everywhere",
        ],
        "incitement_violence": [
            "take up arms storm their homes tonight spill their blood no mercy",
            "hunt them down one by one they deserve to die at our hands",
        ],
        "extremist_propaganda": [
            "advance toward the caliphate the banner rises over every border bayat now",
            "the faithful conquest begins martyrdom is honor obey the call",
        ],
        "terrorist_support": [
            "we praise the glorious attack our heroes struck for us martyr honor",
            "praise the brave fighters heroic operation struck fear for us",
        ],
        "counterspeech": [
            "we condemn the attack in the strongest terms solidarity with victims",
            "standing against hatred and racism one community rejecting bigotry",
        ],
    },
}


def _tf(tokens: list[str]) -> dict[str, float]:
    c: dict[str, int] = {}
    for t in tokens:
        c[t] = c.get(t, 0) + 1
    n = sum(c.values()) or 1
    return {k: v / n for k, v in c.items()}


_STOP = set("the a an and or of to in for on with our their this that is are be as it at by "
            "from we you they i not have has had will would can could all them his her its "
            "في ما من إلى على هذا هذه التي الذي و أو هل قد كل عند مع داخل نحن أن إن".split())


def _centroid_sim(norm_text: str, lang: str) -> dict[str, float]:
    """Cosine-to-nearest-exemplar with a lexical guard: a match only counts if
    >=2 informative (non-stopword) tokens are shared, so formulaic short posts
    can't match hate speech on function-word overlap alone."""
    lang_key = "ar" if lang.startswith("ar") else "en"
    docs = _EXEMPLARS[lang_key]
    tv_tokens = norm_text.split()
    if len(tv_tokens) < 6:
        return {}
    tv = _tf(tv_tokens)
    tv_set = set(tv)
    vecs = {k: [_tf(_anorm(normalize(s)).split()) for s in v] for k, v in docs.items()}
    df: dict[str, int] = {}
    total = 0
    for cat in vecs:
        for d in vecs[cat]:
            total += 1
            for tok in d:
                df[tok] = df.get(tok, 0) + 1
    idf = {t: math.log(1 + total / (1 + c)) for t, c in df.items()}
    rare_floor = 1.5

    def cosine(a: dict[str, float], b: dict[str, float]) -> float:
        common = set(a) & set(b)
        if not common:
            return 0.0
        dot = sum(a[k] * idf.get(k, 0) * b[k] * idf.get(k, 0) for k in common)
        na = math.sqrt(sum((v * idf.get(k, 0)) ** 2 for k, v in a.items()))
        nb = math.sqrt(sum((v * idf.get(k, 0)) ** 2 for k, v in b.items()))
        return dot / (na * nb) if na and nb else 0.0

    out: dict[str, float] = {}
    for cat, ex_list in docs.items():
        for ex in ex_list:
            informative_tv = tv_set - _STOP
            informative_ex = set(normalize(ex).split()) - _STOP
            shared = informative_tv & informative_ex
            strong_shared = [w for w in shared if idf.get(w, 0) >= rare_floor]
            if len(strong_shared) < 2:
                continue
            sim = cosine(tv, _tf(normalize(ex).split()))
            if sim > 0.22:
                out[cat] = max(out.get(cat, 0), min(100.0, sim * 130))
    return out
