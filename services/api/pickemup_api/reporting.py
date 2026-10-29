"""Facts-first analyst reporting + autonomous investigation + email delivery.

Design contract (the honesty rule, made structural):
  1. `build_facts()` turns engine outputs into a compact, provenance-carrying
     fact-pack. It is pure and deterministic — same DB state, same facts.
  2. `compose_template()` renders a full report from facts alone. The product
     therefore NEVER requires an LLM to produce a report.
  3. When an LLM IS connected, it only NARRATES the same fact-pack (strict
     system prompt: no facts invented, cite handles/IDs, keep the caveats).
     Narration is decoration on auditable ground truth, never a source of
     findings. Every report stores its facts + facts_sha256 to be re-audited.
  4. Email delivery is explicit and audited (ReportDelivery). Sending to
     authorities is opt-in per send / per monitor config; the in-app report
     (user copy) is always produced.
"""
from __future__ import annotations

import hashlib
import json
import re
import smtplib
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pickemup_analysis import ALGO_VERSION

from .llm import LLMClient, LLMError, LLMConfig, ToolSpec, get_config
from .models import (Account, Alert, AuditLog, DetectionResult, KV, Post, Report,
                     ReportDelivery, utcnow)

SMTP_KV = "smtp_config"
MONITOR_KV = "monitor_config"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


# ------------------------------------------------------------- fact packing

def _aware(d):
    if d is not None and d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc)
    return d


def _iso(d) -> str | None:
    d = _aware(d)
    return d.isoformat() if d else None


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def build_facts(session: Session, scope: str = "snapshot") -> dict:
    """Deterministic fact-pack. scope: snapshot|alerts|cluster:<id>|account:<id>."""
    origin_mix = dict(session.execute(
        select(Post.origin_mode, func.count()).where(Post.deleted.is_(False))
        .group_by(Post.origin_mode)).all())
    accounts_n = session.scalar(select(func.count(Account.id)).where(Account.deleted.is_(False))) or 0
    posts_n = session.scalar(select(func.count(Post.id)).where(Post.deleted.is_(False))) or 0
    window = session.execute(select(func.min(Post.created_at), func.max(Post.created_at))
                             .where(Post.deleted.is_(False))).one()

    risk_rows = session.scalars(select(DetectionResult).where(
        DetectionResult.kind == "risk").order_by(DetectionResult.score.desc())).all()
    risk_by_cat: dict[str, int] = {}
    for r in risk_rows:
        sev = r.payload.get("severity", "low")
        risk_by_cat[f"{r.verdict}/{sev}"] = risk_by_cat.get(f"{r.verdict}/{sev}", 0) + 1
    risk_top = [{
        "post_id": r.subject_id, "author": r.payload.get("author_id"),
        "category": r.verdict, "severity": r.payload.get("severity"),
        "score": round(r.score or 0, 3), "language": r.payload.get("language"),
        "matched_terms": (r.payload.get("matched_terms") or [])[:5],
        "human_review_required": bool(r.payload.get("human_review_required")),
        "excerpt": (r.payload.get("excerpt") or "")[:180],
        "post_created_at": r.payload.get("post_created_at"),
    } for r in risk_rows if r.payload.get("severity") in ("high", "medium")][:12]

    auto_rows = session.scalars(select(DetectionResult).where(
        DetectionResult.kind == "automation")).all()
    by_id = {a.id: a for a in session.scalars(select(Account)).all()}
    auto_verdicts: dict[str, int] = {}
    for r in auto_rows:
        auto_verdicts[r.verdict or "?"] = auto_verdicts.get(r.verdict or "?", 0) + 1
    auto_top = []
    for r in sorted(auto_rows, key=lambda x: -(x.score or 0)):
        if r.verdict not in ("likely_automated", "mixed_automation") and (r.score or 0) < 0.55:
            continue
        a = by_id.get(r.subject_id)
        fams = sorted({s.get("family", "?") for s in (r.payload.get("signals") or []) if s.get("score", 0) > 0.2})
        auto_top.append({"account": a.username if a else r.subject_id, "id": r.subject_id,
                         "verdict": r.verdict, "score": round(r.score or 0, 3),
                         "confidence": r.confidence,
                         "origin_mode": a.origin_mode if a else None,
                         "signal_families": fams,
                         "evidence_post_ids": (r.payload.get("evidence_post_ids") or [])[:5]})
        if len(auto_top) >= 10:
            break

    cluster_rows = session.scalars(select(DetectionResult).where(
        DetectionResult.kind == "cluster").order_by(DetectionResult.confidence.desc())).all()
    clusters = []
    for r in cluster_rows:
        pl = r.payload
        labels = [by_id[m].username if m in by_id else m for m in (pl.get("members") or [])[:8]]
        clusters.append({"cluster_id": r.subject_id, "verdict": r.verdict,
                         "confidence": round(r.confidence or 0, 3),
                         "accounts": pl.get("accounts"), "edges": pl.get("edge_count"),
                         "signal_mix": pl.get("signal_mix"),
                         "shared_hashtags": (pl.get("shared_hashtags") or [])[:6],
                         "shared_domains": (pl.get("shared_domains") or pl.get("shared_urls") or [])[:4],
                         "central_accounts": [by_id[m].username if m in by_id else m
                                              for m in (pl.get("central_accounts") or [])[:4]],
                         "amplifiers": [by_id[m].username if m in by_id else m
                                        for m in (pl.get("amplifiers") or [])[:4]],
                         "members_sample": labels,
                         "time_range": pl.get("time_range"),
                         "organic_explanations": (pl.get("organic_explanations") or [])[:3],
                         "caveats": (pl.get("caveats") or [])[:3]})
    region_mix = dict(session.execute(
        select(Account.region, func.count()).where(Account.deleted.is_(False))
        .group_by(Account.region)).all())

    nar_rows = session.scalars(select(DetectionResult).where(
        DetectionResult.kind == "narrative")).all()
    narratives = []
    for r in nar_rows[:8]:
        pl = r.payload
        fo = pl.get("first_observed") or {}
        author = by_id.get(fo.get("author_id"))
        narratives.append({"group_id": r.subject_id, "query": pl.get("query"),
                           "posts": len(pl.get("post_ids") or []),
                           "earliest_observed": {
                               "post_id": fo.get("post_id"), "at": fo.get("at"),
                               "author": author.username if author else fo.get("author_id"),
                               "excerpt": (fo.get("text") or "")[:160]},
                           "amplifiers": [by_id[m].username if m in by_id else m
                                          for m in (pl.get("amplifiers") or [])[:5]],
                           "hashtags": (pl.get("hashtags") or [])[:6],
                           "coverage_gaps": (pl.get("coverage_gaps") or [])[:3]})

    alerts = session.scalars(select(Alert).where(Alert.status == "new")
                             .order_by(Alert.id.desc()).limit(25)).all()
    alerts_out = [{"id": a.id, "kind": a.kind, "severity": a.severity,
                   "subject": a.subject_id, "evidence": a.evidence,
                   "created_at": _iso(a.created_at)} for a in alerts]

    # scope narrowing ------------------------------------------------------
    if scope.startswith("cluster:"):
        cid = scope.split(":", 1)[1]
        clusters = [c for c in clusters if c["cluster_id"] == cid]
        ids = set((clusters[0]["members_sample"] if clusters else []))
        auto_top = [a for a in auto_top if a["account"] in ids or a["id"] == cid]
        focus = None  # cluster reports narrow the lists; no single-account dossier block
    elif scope.startswith("account:"):
        key = scope.split(":", 1)[1]
        acct = by_id.get(key) or next((a for a in by_id.values() if a.username == key), None)
        if not acct:
            raise ValueError(f"unknown account '{key}'")
        auto_top = [a for a in auto_top if a["id"] == acct.id]
        clusters = [c for c in clusters if acct.username in (c.get("members_sample") or [])]
        recent = session.scalars(select(Post).where(Post.author_id == acct.id, Post.deleted.is_(False))
                                 .order_by(Post.created_at.desc()).limit(25)).all()
        focus = {"username": acct.username, "id": acct.id, "origin_mode": acct.origin_mode,
                 "region": acct.region, "verified": acct.verified,
                 "followers": acct.followers, "following": acct.following,
                 "first_seen": _iso(acct.first_seen),
                 "recent_posts": [{"id": p.id, "at": _iso(p.created_at),
                                   "excerpt": (p.text or "")[:160],
                                   "hashtags": p.hashtags, "mentions": p.mentions,
                                   "is_repost": bool(p.repost_of)} for p in recent]}
    else:
        focus = None

    return {
        "generated_at": utcnow().isoformat(),
        "algo_version": ALGO_VERSION,
        "scope": scope,
        "corpus": {"accounts": accounts_n, "posts": posts_n,
                   "origin_mix": {str(k): v for k, v in origin_mix.items()},
                   "collection_window": {"from": _iso(window[0]), "to": _iso(window[1])},
                   "region_mix": {str(k): v for k, v in region_mix.items()}},
        "content_risk": {"by_category_severity": risk_by_cat, "flagged": len(risk_rows),
                         "actionable_top": risk_top},
        "automation": {"verdict_counts": auto_verdicts, "top_suspects": auto_top},
        "coordination": {"clusters": clusters},
        "narratives": narratives,
        "alerts_new": alerts_out,
        "focus_account": focus,
        "provenance_notes": [
            "origin_mix: 'synthetic'/'mock' rows are campaign-simulation fixtures, not live platforms",
            "content severity is separate from identity, affiliation, or intent",
            "'earliest observed' is NOT the true origin when collection history is incomplete",
            "clusters group accounts by coordination signals, not by identity or opinion",
        ],
    }


# ------------------------------------------------- deterministic template writer

_H = {  # header strings en/ar
    "en": {"title": "PICKEMUP Intelligence Report", "summary": "Executive summary",
           "risk": "Content risk", "auto": "Automation likelihood", "coord": "Coordination clusters",
           "nar": "Narrative propagation", "alerts": "Open alerts", "integrity": "Evidence integrity",
           "focus": "Focus account", "corpus": "Corpus", "generated": "Generated",
           "mode": "Narrative mode", "facts": "Facts fingerprint",
           "disclaimer": ("This report is generated from deterministic engine outputs over stored "
                          "records. It is analytical inference, not proof of identity, affiliation, "
                          "or intent. Review flagged items with a human before any external action.")},
    "ar": {"title": "تقرير استخباراتي PICKEMUP", "summary": "الملخص التنفيذي",
           "risk": "مخاطر المحتوى", "auto": "احتمالية التشغيل الآلي", "coord": "شبكات التنسيق",
           "nar": "انتشار السرد", "alerts": "تنبيهات مفتوحة", "integrity": "سلامة الأدلة",
           "focus": "الحساب المستهدف", "corpus": "قاعدة البيانات", "generated": "تاريخ الإصدار",
           "mode": "طريقة السرد", "facts": "بصمة الحقائق",
           "disclaimer": ("هذا التقرير مبني على مخرجات محركات حتمية فوق سجلات محفوظة، وهو استنتاج "
                          "تحليلي وليس دليلاً على هوية أو انتماء أو نية. راجع البنود المعلَّمة بشريًا "
                          "قبل أي إجراء خارجي.")},
}


def compose_template(f: dict, language: str = "en") -> str:
    t = _H.get(language, _H["en"])
    L: list[str] = []
    c, risk, auto, coord = f["corpus"], f["content_risk"], f["automation"], f["coordination"]
    L.append(f"# {t['title']} — {f['scope']}")
    L.append(f"*{t['generated']}: {f['generated_at'][:19]}Z · {t['mode']}: deterministic · "
             f"algo `{f['algo_version']}`*")
    L.append(f"\n## {t['summary']}")
    L.append(f"- {t['corpus']}: **{c['accounts']}** accounts / **{c['posts']}** posts "
             f"({', '.join(f'{k}: {v}' for k, v in c['origin_mix'].items()) or 'no data'})")
    hi = sum(v for k, v in risk["by_category_severity"].items() if "/high" in k)
    L.append(f"- {t['risk']}: **{risk['flagged']}** flagged posts, **{hi}** high-severity "
             f"(human review required where marked)")
    L.append(f"- {t['auto']}: " + (", ".join(f"{k}: {v}" for k, v in auto["verdict_counts"].items()) or "none scored"))
    L.append(f"- {t['coord']}: **{len(coord['clusters'])}** cluster(s); "
             f"strongest: {coord['clusters'][0]['cluster_id']} "
             f"(conf {coord['clusters'][0]['confidence']}, {coord['clusters'][0]['accounts']} accounts)"
             if coord["clusters"] else f"- {t['coord']}: none detected")
    L.append(f"- {t['alerts']}: **{len(f['alerts_new'])}** open")

    if f.get("focus_account"):
        a = f["focus_account"]
        L.append(f"\n## {t['focus']}: @{a['username']}")
        L.append(f"- origin `{a['origin_mode']}` · region {a.get('region') or '—'} · "
                 f"followers {a.get('followers') or '?'} · first seen {str(a.get('first_seen'))[:19]}")
        for p in (a.get("recent_posts") or [])[:10]:
            tag = " ⟳" if p["is_repost"] else ""
            L.append(f"  - `{p['id'][:16]}`{tag} {str(p['at'])[:16]}: {p['excerpt'][:120]}")

    if risk["actionable_top"]:
        L.append(f"\n## {t['risk']}")
        for r in risk["actionable_top"]:
            flag = " ⚑review" if r["human_review_required"] else ""
            L.append(f"- **{r['category']}/{r['severity']}** score {r['score']} · "
                     f"@{r['author']} · `{r['post_id'][:16]}`{flag} · terms {r['matched_terms']}")
            L.append(f"  > {r['excerpt']}")
    if auto["top_suspects"]:
        L.append(f"\n## {t['auto']}")
        for a in auto["top_suspects"]:
            L.append(f"- **@{a['account']}** — {a['verdict']} ({a['score']}), "
                     f"families: {', '.join(a['signal_families']) or '—'}, "
                     f"origin `{a['origin_mode']}`, evidence posts {a['evidence_post_ids'][:3]}")
    if coord["clusters"]:
        L.append(f"\n## {t['coord']}")
        for cl in coord["clusters"]:
            L.append(f"### {cl['cluster_id']} — {cl['verdict']} (conf {cl['confidence']})")
            L.append(f"- {cl['accounts']} accounts, {cl['edges']} edges; signals: {cl['signal_mix']}")
            L.append(f"- central: {', '.join('@' + m for m in cl['central_accounts']) or '—'}; "
                     f"amplifiers: {', '.join('@' + m for m in cl['amplifiers']) or '—'}")
            if cl["shared_hashtags"]:
                L.append(f"- shared hashtags: {', '.join('#' + h for h in cl['shared_hashtags'])}")
            if cl["organic_explanations"]:
                L.append(f"- benign explanations considered: {'; '.join(cl['organic_explanations'])}")
            if cl["caveats"]:
                L.append(f"- caveats: {'; '.join(cl['caveats'])}")
    if f["narratives"]:
        L.append(f"\n## {t['nar']}")
        for n in f["narratives"]:
            fo = n["earliest_observed"]
            L.append(f"- query **{n['query']}** ({n['posts']} posts) — earliest observed "
                     f"{str(fo['at'])[:16]} by @{fo['author']} `{str(fo['post_id'])[:16]}`")
            L.append(f"  > {fo['excerpt']}")
            if n["coverage_gaps"]:
                L.append(f"  - gaps: {'; '.join(n['coverage_gaps'])}")
    if f["alerts_new"]:
        L.append(f"\n## {t['alerts']}")
        for a in f["alerts_new"]:
            L.append(f"- [{a['severity']}] {a['kind']} — {a['subject']} ({str(a['created_at'])[:16]})")

    L.append(f"\n## {t['integrity']}")
    L.append(f"- facts_sha256: `{f.get('_sha') or _sha(f)}` · algo_version: `{f['algo_version']}`")
    for n in f["provenance_notes"]:
        L.append(f"- {n}")
    L.append(f"\n> {t['disclaimer']}")
    return "\n".join(L)


# ------------------------------------------------------------- LLM narrator

NARRATE_SYSTEM = (
    "You are the report writer inside PICKEMUP, a local-first OSINT console. "
    "You receive a FACTS JSON block produced by deterministic detection engines. "
    "Rules you MUST follow: (1) use ONLY the provided facts — never invent accounts, "
    "posts, events, dates, or external knowledge; (2) cite concrete handles (@name), "
    "ids, and counts for every claim; (3) preserve every caveat and provenance note — "
    "hedged language is a feature; (4) never assert identity, affiliation, intent, or "
    "true origins; 'earliest observed' is not 'original'; (5) synthetic/mock origin "
    "data is simulation fixture, label it as such. "
    "Output clean Markdown with sections: Title, Executive summary (5 bullets), "
    "Key findings (grouped), Assessment (confidence + what would change it), "
    "Recommended next investigative steps, Evidence integrity. "
    "Do not add a sources section; ids are inline."
)

NARRATE_SYSTEM_AR = (
    "أنت كاتب التقارير في نظام PICKEMUP. تستقبل كتلة FACTS من محركات حتمية. "
    "قواعد ملزمة: (1) استخدم الحقائق المقدمة فقط ولا تخترع حسابات أو منشورات أو أحداثًا؛ "
    "(2) استشهد بالمعرّفات والأرقام في كل ادعاء؛ (3) احتفظ بكل التحفظات وملاحظات المصدر؛ "
    "(4) لا تجزم بهوية أو انتماء أو نية، و«أقدم رصد» لا تعني «المصدر الأصلي»؛ "
    "(5) البيانات ذات المنشأ synthetic/mock هي محاكاة. "
    "المخرجات Markdown بأقسام: عنوان، ملخص تنفيذي (٥ نقاط)، النتائج الرئيسية، التقييم، "
    "خطوات تحقق مقترحة، سلامة الأدلة. اكتب بالعربية الفصحى."
)


def _fit_json(payload, limit: int) -> tuple[str, bool]:
    """Serialize to JSON that FITS in `limit` by trimming list tails and long
    strings — never raw-slice mid-JSON and hand the model broken syntax.
    Returns (blob, was_trimmed)."""
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    if len(blob) <= limit:
        return blob, False
    data = json.loads(blob)

    def shrink(obj, cap: int):
        if isinstance(obj, dict):
            return {k: shrink(v, cap) for k, v in obj.items()}
        if isinstance(obj, list):
            return [shrink(x, cap) for x in obj[:cap]]
        if isinstance(obj, str) and len(obj) > 240:
            return obj[:240] + "…"
        return obj

    for cap in (20, 8, 3, 1):
        trimmed = shrink(data, cap)
        if isinstance(trimmed, dict):
            trimmed["_truncated"] = f"payload trimmed to fit model context (list cap {cap})"
        blob = json.dumps(trimmed, ensure_ascii=False, default=str)
        if len(blob) <= limit:
            return blob, True
    return blob[:limit], True


def narrate(cfg: LLMConfig, facts: dict, language: str) -> str:
    client = LLMClient(cfg)
    system = NARRATE_SYSTEM_AR if language == "ar" else NARRATE_SYSTEM
    user = "FACTS JSON:\n" + _fit_json(facts, 60000)[0]
    md = client.complete(system, user)
    if len(md.strip()) < 80:
        raise LLMError("narration came back empty/too short")
    return md.strip()


# ---------------------------------------------------------------- generation

def generate_report(session: Session, scope: str = "snapshot", language: str = "en",
                    use_llm: bool = True, created_by: str = "analyst") -> Report:
    facts = build_facts(session, scope)
    facts["_sha"] = _sha(facts)
    cfg = get_config(session)
    mode, md, llm_err, model = "template", None, None, None
    if use_llm and cfg.usable:
        try:
            md = narrate(cfg, facts, language)
            mode, model = "llm", cfg.model
        except LLMError as e:
            llm_err = str(e)[:500]  # honest fallback: report still produced, error recorded
    if md is None:
        md = compose_template(facts, language)
    title = f"PICKEMUP report — {scope}"
    rep = Report(id=f"rpt-{uuid.uuid4().hex[:10]}", scope=scope, title=title,
                 language=language, mode=mode, model=model, content_md=md,
                 facts={k: v for k, v in facts.items() if k != "_sha"},
                 facts_sha256=facts["_sha"], llm_error=llm_err, created_by=created_by)
    session.add(rep)
    session.add(AuditLog(actor="api", action="ai.report.generate", subject=rep.id,
                         detail={"scope": scope, "mode": mode, "language": language,
                                 "facts_sha": facts["_sha"][:16], "llm_error": bool(llm_err)}))
    session.commit()
    return rep


# -------------------------------------------------------------------- email

def smtp_cfg(session: Session) -> dict:
    row = session.get(KV, SMTP_KV)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def md_to_html(md: str) -> str:
    """Tiny safe Markdown->HTML (escape-first; headings, bold, italics, code, lists, quotes)."""
    import html as _h
    out: list[str] = []
    in_ul = False
    for line in md.splitlines():
        raw = line.rstrip()
        if not raw.strip():
            if in_ul:
                out.append("</ul>")
                in_ul = False
            continue
        esc = _h.escape(raw)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        esc = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", esc)
        esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
        m = re.match(r"^(#{1,4})\s+(.*)$", esc)
        if m:
            lvl = len(m.group(1))
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f"<h{lvl}>{m.group(2)}</h{lvl}>")
            continue
        if raw.lstrip().startswith(("- ", "* ")) and not raw.startswith("  "):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{esc.lstrip('-* ').strip()}</li>")
            continue
        if raw.lstrip().startswith("> "):
            out.append(f"<blockquote>{esc.lstrip('> ').strip()}</blockquote>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        out.append(f"<p>{esc}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def validate_recipients(emails: list[str]) -> list[str]:
    clean = []
    for e in emails:
        e = (e or "").strip().lower()
        if not e:
            continue
        if not EMAIL_RE.match(e):
            raise ValueError(f"invalid email address: {e!r}")
        clean.append(e)
    if not clean:
        raise ValueError("no recipients given")
    return clean


def send_report_email(session: Session, rep: Report, recipients: list[str],
                      recipient_kind: str = "user") -> ReportDelivery:
    cfg = smtp_cfg(session)
    to = validate_recipients(recipients)
    if not cfg.get("host") or not cfg.get("from"):
        raise RuntimeError("SMTP not configured (Settings → Email: host + from required)")
    msg = EmailMessage()
    msg["Subject"] = f"[PICKEMUP · {recipient_kind}] {rep.title}"
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(to)
    intro = ("This PICKEMUP report was delivered by the console you operate. "
             "Findings are analytical inferences from stored records — not proof of "
             "identity, affiliation, or intent. Verify before acting.\n\n")
    msg.set_content(intro + rep.content_md)
    msg.add_alternative(
        f"<html><body><p style='color:#a33'>{intro.replace(chr(10), ' ')}</p>"
        + md_to_html(rep.content_md) + "</body></html>", subtype="html")
    d = ReportDelivery(report_id=rep.id, channel="email", recipients=to,
                       recipient_kind=recipient_kind, status="sent", detail="")
    try:
        host, port = cfg["host"], int(cfg.get("port", 587))
        if int(cfg.get("port", 587)) == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                if cfg.get("username"):
                    s.login(cfg["username"], cfg.get("password", ""))
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                if cfg.get("tls", True):
                    s.starttls()
                if cfg.get("username"):
                    s.login(cfg["username"], cfg.get("password", ""))
                s.send_message(msg)
        d.detail = (msg.get("Message-ID") or "sent")[:200]
    except Exception as e:  # noqa: BLE001 — delivery failure must be recorded, not crash the API
        d.status, d.detail = "failed", f"{type(e).__name__}: {e}"[:500]
    session.add(d)
    session.add(AuditLog(actor="api", action=f"ai.email.{recipient_kind}", subject=rep.id,
                         detail={"recipients": to, "status": d.status}))
    session.commit()
    return d


# ----------------------------------------------------------- investigator

INVESTIGATOR_SYSTEM = (
    "You are PICKEMUP's autonomous investigator. You explore the console's OWN "
    "deterministic findings via the provided read-only tools, then write a final "
    "Markdown investigation note. Rules: call tools with small precise queries; "
    "never invent data — every claim cites what a tool returned (handle, id, count); "
    "state confidence and what you could NOT verify; respect caveats (synthetic/mock "
    "fixtures, 'earliest observed' ≠ origin, no identity/intent claims). "
    "Use at most 6 tool calls. When done, answer in plain Markdown WITHOUT further tool calls."
)

MAX_TOOL_RESULT_CHARS = 4000


def investigator_tools(session: Session) -> list[ToolSpec]:
    def _uname(a_id):
        a = session.get(Account, a_id)
        return a.username if a else a_id

    # snapshot built ONCE per investigation: every read-only tool used to
    # rebuild the entire fact-pack (all detections + accounts) on every call
    _facts_cache: dict[str, dict] = {}

    def _facts() -> dict:
        if not _facts_cache:
            _facts_cache["f"] = build_facts(session, "snapshot")
        return _facts_cache["f"]

    def corpus_stats() -> dict:
        f = _facts()
        return {"corpus": f["corpus"], "risk_counts": f["content_risk"]["by_category_severity"],
                "automation_counts": f["automation"]["verdict_counts"],
                "clusters": len(f["coordination"]["clusters"]),
                "open_alerts": len(f["alerts_new"])}

    def search_posts(query: str, limit: int = 8) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        rows = session.scalars(select(Post).where(Post.deleted.is_(False), Post.text.ilike(like))
                               .order_by(Post.created_at.desc())
                               .limit(min(int(limit or 8), 25))).all()
        return [{"id": p.id, "author": _uname(p.author_id), "at": _iso(p.created_at),
                 "lang": p.lang, "hashtags": p.hashtags, "excerpt": (p.text or "")[:200],
                 "origin": p.origin_mode, "is_repost": bool(p.repost_of)} for p in rows]

    def account_findings(handle: str) -> dict:
        a = next((x for x in session.scalars(select(Account)).all()
                  if x.id == handle or x.username == handle.lstrip("@")) , None)
        if not a:
            return {"error": f"unknown account {handle}"}
        det = {}
        for kind in ("automation",):
            r = session.scalar(select(DetectionResult).where(DetectionResult.kind == kind,
                                                             DetectionResult.subject_id == a.id))
            if r:
                det[kind] = {"verdict": r.verdict, "score": r.score,
                             "families": sorted({s.get("family", "?") for s in (r.payload.get("signals") or [])
                                                 if s.get("score", 0) > 0.2}),
                             "evidence_post_ids": (r.payload.get("evidence_post_ids") or [])[:6]}
        posts = session.scalars(select(Post).where(Post.author_id == a.id, Post.deleted.is_(False))
                                .order_by(Post.created_at.desc()).limit(12)).all()
        risks = session.scalars(select(DetectionResult).where(DetectionResult.kind == "risk",
                              DetectionResult.subject_id.in_([p.id for p in posts] or ["-"]))).all()
        return {"account": {"username": a.username, "id": a.id, "origin": a.origin_mode,
                            "region": a.region, "verified": a.verified,
                            "followers": a.followers, "first_seen": _iso(a.first_seen)},
                "detections": det,
                "recent_posts": [{"id": p.id, "at": _iso(p.created_at),
                                  "excerpt": (p.text or "")[:160]} for p in posts],
                "flagged_posts": [{"post_id": r.subject_id, "category": r.verdict,
                                   "severity": r.payload.get("severity")} for r in risks]}

    def list_clusters() -> list[dict]:
        f = _facts()
        return [{"cluster_id": c["cluster_id"], "verdict": c["verdict"], "confidence": c["confidence"],
                 "accounts": c["accounts"], "signals": c["signal_mix"],
                 "hashtags": c["shared_hashtags"], "members_sample": c["members_sample"]}
                for c in f["coordination"]["clusters"]]

    def cluster_detail(cluster_id: str) -> dict:
        r = session.scalar(select(DetectionResult).where(DetectionResult.kind == "cluster",
                                                         DetectionResult.subject_id == cluster_id))
        if not r:
            return {"error": f"unknown cluster {cluster_id}"}
        f = _facts()
        return next((c for c in f["coordination"]["clusters"] if c["cluster_id"] == cluster_id),
                    {"error": "cluster not in snapshot"})

    def narrative_trace(query: str) -> list[dict]:
        f = _facts()
        q = (query or "").lower()
        return [n for n in f["narratives"] if q in str(n.get("query", "")).lower()][:5]

    def recent_alerts(severity: str = "") -> list[dict]:
        f = _facts()
        return [a for a in f["alerts_new"] if not severity or a["severity"] == severity][:15]

    return [
        ToolSpec("corpus_stats", "Overall counts: accounts, posts, origin mix, risk/automation "
                                 "totals, cluster and alert counts.", {}, corpus_stats),
        ToolSpec("search_posts", "Full-text search over collected post text.",
                 {"type": "object", "properties": {
                     "query": {"type": "string"}, "limit": {"type": "integer"}},
                  "required": ["query"]}, search_posts),
        ToolSpec("account_findings", "One account: profile facts, automation verdict + signal "
                                     "families, recent posts, flagged posts.",
                 {"type": "object", "properties": {"handle": {"type": "string"}},
                  "required": ["handle"]}, account_findings),
        ToolSpec("list_clusters", "All coordination clusters with confidence and signal mix.",
                 {}, list_clusters),
        ToolSpec("cluster_detail", "Full detail for one cluster (members, hashtags, urls, "
                                   "amplifiers, caveats).",
                 {"type": "object", "properties": {"cluster_id": {"type": "string"}},
                  "required": ["cluster_id"]}, cluster_detail),
        ToolSpec("narrative_trace", "Narrative propagation records matching a query/tag.",
                 {"type": "object", "properties": {"query": {"type": "string"}},
                  "required": ["query"]}, narrative_trace),
        ToolSpec("recent_alerts", "Open alerts (optionally filtered by severity).",
                 {"type": "object", "properties": {"severity": {"type": "string"}}},
                 recent_alerts),
    ]


def _execute_tool(tools: list[ToolSpec], name: str, args: dict) -> Any:
    for t in tools:
        if t.name == name:
            try:
                return t.fn(**(args or {}))
            except TypeError as e:
                return {"error": f"bad arguments for {name}: {e}"}
            except Exception as e:  # noqa: BLE001 — feed tool errors back to the model
                return {"error": f"{type(e).__name__}: {e}"}
    return {"error": f"unknown tool '{name}'"}


def investigate(session: Session, focus: str, max_steps: int = 6) -> Report:
    cfg = get_config(session)
    if not cfg.usable:
        raise LLMError("the autonomous investigator requires a connected LLM "
                       "(Settings → LLM); template reports work without one")
    client = LLMClient(cfg)
    tools = investigator_tools(session)
    messages: list[dict] = [
        {"role": "system", "content": INVESTIGATOR_SYSTEM},
        {"role": "user", "content": f"Investigation focus: {focus[:500] or 'open monitoring — find the '
                                              'most significant recent anomaly across automation, '
                                              'coordination, and narrative findings.'}"},
    ]
    steps: list[dict] = []
    final_md = ""
    for _ in range(max_steps + 2):
        resp = client.chat(messages, tools=tools)
        if resp["kind"] == "answer":
            final_md = resp["content"].strip()
            break
        name, args = resp["name"], resp["arguments"]
        result = _execute_tool(tools, name, args)
        blob, was_trimmed = _fit_json(result, MAX_TOOL_RESULT_CHARS)
        steps.append({"tool": name, "args": args, "result_chars": len(blob),
                      "result_head": blob[:300],  # auditable: what the model actually read
                      "truncated": was_trimmed,
                      "error": bool(isinstance(result, dict) and result.get("error"))})
        # echo the provider's assistant message verbatim (carries tool_calls);
        # posting a dict as `content` would be a protocol violation
        am = resp.get("raw_message")
        messages.append(am if isinstance(am, dict) and am.get("role") == "assistant"
                        else {"role": "assistant", "content": None,
                              "tool_calls": [{"id": name, "type": "function",
                                              "function": {"name": name,
                                                           "arguments": json.dumps(args)}}]})
        messages.append({"role": "tool", "tool_call_id": (resp.get("raw_message") or {})
                         .get("tool_calls", [{}])[0].get("id", name),
                         "name": name, "content": blob})
    if not final_md:
        messages.append({"role": "user", "content": "Tool budget reached. Write the final "
                                                    "Markdown investigation note NOW from what "
                                                    "you have gathered, stating confidence and gaps."})
        final_md = client.chat(messages)["content"].strip()
    facts = {"focus": focus, "tool_steps": steps, "algo_version": ALGO_VERSION}
    sha = _sha(facts)
    rep = Report(id=f"rpt-{uuid.uuid4().hex[:10]}", scope=f"investigation:{focus[:80]}",
                 title=f"Investigation — {focus[:60] or 'auto-anomaly'}",
                 language="en", mode="llm", model=cfg.model,
                 content_md=final_md or "_(empty model response)_",
                 facts=facts, facts_sha256=sha, created_by="analyst")
    session.add(rep)
    session.add(AuditLog(actor="api", action="ai.investigate", subject=rep.id,
                         detail={"focus": focus[:120], "steps": len(steps), "facts_sha": sha[:16]}))
    session.commit()
    return rep
