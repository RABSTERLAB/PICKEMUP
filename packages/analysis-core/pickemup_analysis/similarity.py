"""Deterministic near-duplicate text similarity: word shingles + MinHash.

Near-duplicate detection is the established technique family for
use for 'similar content' edges. We use pure-Python MinHash over 3-word
shingles so results are reproducible, offline, and language-agnostic at the
token level (works for Arabic and English alike; Arabizi de-arabi-zi is
handled by normalization below).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

_NUM63 = (1 << 63) - 1
_PRIME = (1 << 61) - 1
_SEEDS = [(a, b) for a, b in ((i * 6364136223846793005 + 1442695040888963407,
                               i * 1442695040888963407 + 6364136223846793005)
                              for i in range(16))]

_WS = re.compile(r"\s+")
# strip punctuation except # (hashtags) and @ (mentions) and Arabic marks that matter
_PUNCT = re.compile(r"[^\w#@ً-ٰٟ%]+", re.UNICODE)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = t.lower()
    # drop URLs/mentions noise but keep hashtag words
    t = re.sub(r"https?://\S+", " urltok ", t)
    t = _PUNCT.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    return t


def shingles(text: str, k: int = 3) -> set[str]:
    if int(k) < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    k = int(k)
    toks = normalize(text).split()
    if not toks:
        return set()
    if len(toks) <= k:
        return {" ".join(toks)}
    return {" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1)}


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big") & _NUM63


def minhash(text: str, num_perm: int = 16) -> tuple[int, ...]:
    if not 1 <= int(num_perm) <= len(_SEEDS):
        raise ValueError(f"num_perm must be 1..{len(_SEEDS)} (seed table size), got {num_perm}")
    num_perm = int(num_perm)
    sh = shingles(text)
    if not sh:
        return ()
    base = [_h64(s) for s in sh]  # min() is order-invariant; no need to sort per post
    out = []
    for a, b in _SEEDS[:num_perm]:
        out.append(min(((x * a + b) % _PRIME) & _NUM63 for x in base))
    return tuple(out)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a) + len(b) - inter
    return inter / union if union else 0.0


def minhash_jaccard(m1: tuple[int, ...], m2: tuple[int, ...]) -> float:
    """Estimate of Jaccard similarity of the underlying shingle sets."""
    if not m1 or not m2:
        return 0.0
    n = min(len(m1), len(m2))
    return sum(1 for i in range(n) if m1[i] == m2[i]) / n


def text_similarity(text_a: str, text_b: str) -> float:
    """Exact-shingle Jaccard (small inputs). Use minhash variants at scale."""
    return jaccard(shingles(text_a), shingles(text_b))
