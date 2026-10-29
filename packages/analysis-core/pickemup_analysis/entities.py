"""Typed entity extraction from post text — the local entity index core.

A typed event store: every observation is classified and linked back to its
source record, following standard OSINT entity taxonomies. Deterministic and
regex-based; no ML, no network. Values are normalized (lowercase for latin;
Arabic script preserved) so pivots match consistently.
"""
from __future__ import annotations

import re

# Arabic-capable word char for hashtags/mentions
_AW = r"A-Za-z0-9_\u0600-\u06FF"

URL_RE = re.compile(r"(?<![\w/])(?:https?://|www\.)[^\s\"'<>()\[\]{}]+", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
PHONE_RE = re.compile(r"(?<![\d+])\+?\d[\d\- ()]{8,16}\d(?!\d)")
HASHTAG_RE = re.compile(rf"(?<![\w&])#([{_AW}][{_AW}*.]{{1,63}})", re.UNICODE)
MENTION_RE = re.compile(rf"(?<![\w/])@([a-zA-Z0-9_]{{1,30}}(?:\.[a-zA-Z0-9_]{{1,30}})?)")
_DOMAIN_RE = re.compile(r"^(?:https?://)?(www\.)?([a-z0-9À-ʸ\-]+(?:\.[a-z]{2,}){1,3})", re.I)


def _norm_domain(host: str) -> str:
    host = host.lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def extract_entities(text: str) -> dict[str, list[str]]:
    """{entity_type: [values]} for one post body. Types:
    URL, DOMAIN, HASHTAG, MENTION, EMAIL, PHONE."""
    t = text or ""
    out: dict[str, list[str]] = {"URL": [], "DOMAIN": [], "HASHTAG": [],
                                 "MENTION": [], "EMAIL": [], "PHONE": []}
    seen: dict[str, set[str]] = {k: set() for k in out}

    def add(kind: str, val: str) -> None:
        v = val.strip().rstrip(".,;:!?،؛")
        if v and v.lower() not in seen[kind]:
            seen[kind].add(v.lower())
            out[kind].append(v)

    for m in URL_RE.finditer(t):
        raw = m.group(0)
        add("URL", raw)
        host = raw.split("//", 1)[-1].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        host = host.rsplit("@", 1)[-1].split(":", 1)[0]  # drop userinfo, then port
        dm = _DOMAIN_RE.fullmatch(host) if host else None
        if dm:
            add("DOMAIN", _norm_domain(dm.group(2)))
    t_no_url = URL_RE.sub(" ", t)  # URL userinfo (https://user:tok@host/) is not an email
    for m in EMAIL_RE.finditer(t_no_url):
        add("EMAIL", m.group(0).lower())
    for m in HASHTAG_RE.finditer(t):
        add("HASHTAG", m.group(1).lower())
    for m in MENTION_RE.finditer(t):
        add("MENTION", m.group(1).lower())
    for m in PHONE_RE.finditer(t):
        digits = re.sub(r"\D", "", m.group(0))
        if 9 <= len(digits) <= 15:
            add("PHONE", digits)  # normalized digits: format variants must merge
    return {k: v for k, v in out.items() if v}


def all_types() -> list[str]:
    return ["URL", "DOMAIN", "HASHTAG", "MENTION", "EMAIL", "PHONE"]
