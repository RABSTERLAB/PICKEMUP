"""Shared helpers for the operator-extensible trigger-word lexicon.

The curated moderation MATRIX stays in code (reviewed, versioned); user terms
live in the DB (CustomTerm) and are injected into every content-risk run, so
the watchlist is immediately corpus-wide and survives restarts. The engine
package stays pure (no DB import); injection happens here in the API layer.
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pickemup_analysis.risk import MATRIX, WATCHLIST_CAT  # re-export: single name

from .models import CustomTerm, Post

# Severity tiers map onto the existing action bands of the moderation matrix:
#   watch  (0.45) -> ACTION_FLAG_HUMAN_REVIEW  (0.40–0.79 band)
#   flag   (0.65) -> ACTION_FLAG_HUMAN_REVIEW, higher rank/score
#   severe (0.85) -> ACTION_BLOCK band         (>=0.80), human review required
SEVERITY_WEIGHT = {"watch": 0.45, "flag": 0.65, "severe": 0.85}

TERM_RE = re.compile(r"^[\w \.\-\!\؟?ء-ي\u0600-\u06FF+]{1,78}$", re.UNICODE)


def load_custom_terms(session: Session) -> list[CustomTerm]:
    return list(session.scalars(select(CustomTerm).where(CustomTerm.enabled.is_(True))).all())


def matrix_terms() -> dict[str, int]:
    """Built-in coverage summary: category -> term count."""
    return {cat: len(terms) for cat, terms in MATRIX.items()}


def search_matrix(query: str, limit: int = 25) -> list[dict]:
    """Substring search over the built-in matrix (‘is this word already covered?’)."""
    q = (query or "").strip().lower()
    if not q:
        return []
    out: list[dict] = []
    for cat, terms in MATRIX.items():
        for t, w in terms.items():
            if q in t.lower():
                out.append({"term": t, "category": cat, "weight": round(float(w), 2), "source": "matrix"})
                if len(out) >= limit:
                    return out
    return out


def _like_pat(term: str) -> str:
    t = (term or "").strip().lower()
    return "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def corpus_hits(session: Session, term: str) -> int:
    """How many stored (non-deleted) posts contain this term, case-insensitive."""
    t = (term or "").strip().lower()
    if not t:
        return 0
    return session.scalar(
        select(func.count(Post.id)).where(
            Post.deleted.is_(False),
            func.lower(Post.text).like(_like_pat(t), escape="\\"))) or 0


def corpus_hits_many(session: Session, terms: list[str]) -> dict[str, int]:
    """Hit counts for MANY terms in ONE corpus scan — list views used to run
    one unindexable LIKE full-scan per watchlist row."""
    terms = [t for t in ((x or "").strip().lower() for x in terms) if t]
    if not terms:
        return {}
    from sqlalchemy import case
    cols = [func.sum(case((func.lower(Post.text).like(_like_pat(t), escape="\\"), 1),
                          else_=0)) for t in terms]
    row = session.execute(select(*cols).where(Post.deleted.is_(False))).one()
    return {t: int(v or 0) for t, v in zip(terms, row)}
