"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, type RiskFeed, type Overview } from "@/lib/api";

const CAT_LABELS: Record<string, string> = {
  extremist_propaganda: "Extremist Propaganda", hate_speech: "Hate Speech",
  incitement_violence: "Incitement to Violence", racism: "Racism",
  terrorist_support: "Terrorist Support", counterspeech: "Counterspeech",
  watchlist_match: "⚑ Watchlist Term", benign: "Benign",
};
const THREAT_GLOW: Record<string, string> = {
  critical: "glow-crimson", high: "glow-crimson", moderate: "glow-amber", low: "glow-teal",
};

/* -------- operator-extensible trigger-word watchlist -------- */
type LexTerm = { id: number; term: string; severity: string; note: string; enabled: boolean;
  weight: number; hits: number; created_by: string; created_at: string };
type Lexicon = { custom: LexTerm[]; built_in: { categories: Record<string, number>; total_terms: number };
  severity_weights: Record<string, number>; active_terms: number };
const SEV_TIP: Record<string, string> = {
  watch: "flagged for human review (0.45)", flag: "flagged, higher rank (0.65)", severe: "block band (0.85)",
};

function Watchlist({ bump }: { bump: () => void }) {
  const [lx, setLx] = useState<Lexicon | null>(null);
  const [open, setOpen] = useState(true);  // discoverable by default: this is the "teach new words" entry point
  const [term, setTerm] = useState("");
  const [sev, setSev] = useState("watch");
  const [note, setNote] = useState("");
  const [q, setQ] = useState("");
  const [found, setFound] = useState<{ built_in: { term: string; category: string; weight: number }[];
    watchlist: LexTerm[] } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.get<Lexicon>("/api/v1/lexicon").then(setLx).catch(() => setLx(null));
  }, []);
  useEffect(() => { load(); }, [load]);

  const run = async (fn: () => Promise<string | null>) => {
    setBusy(true); setMsg(null);
    try { const m = await fn(); if (m) setMsg(m); } catch (e) { setMsg(String(e).replace(/^Error: \d+ /, "")); }
    load(); bump(); setBusy(false);
  };
  const add = () => run(async () => {
    if (!term.trim()) return null;
    const r = await api.post<{ already_in_matrix: boolean }>("/api/v1/lexicon",
      { term: term.trim(), severity: sev, note });
    setTerm(""); setNote("");
    return r.already_in_matrix
      ? "Added — note: this term was ALREADY in the built-in matrix (yours now adds an explicit watch rule on top)."
      : "Term added — corpus re-scoring queued; hits appear below and on the feed within seconds.";
  });
  const search = () => run(async () => {
    if (!q.trim()) return null;
    const r = await api.get<{ built_in: { term: string; category: string; weight: number }[]; watchlist: LexTerm[] }>(
      `/api/v1/lexicon/search?q=${encodeURIComponent(q.trim())}`);
    setFound(r);
    return null;
  });
  const toggle = (t: LexTerm) => run(() => api.patch(`/api/v1/lexicon/${t.id}`, { enabled: !t.enabled }).then(() => null));
  const remove = (t: LexTerm) => run(() => api.del(`/api/v1/lexicon/${t.id}`).then(() => null));

  const totalHits = (lx?.custom ?? []).filter((t) => t.enabled).reduce((a, t) => a + t.hits, 0);

  return (
    <div className="card glass">
      <button className="w-full flex items-center gap-2 text-left" style={{ cursor: "pointer" }}
        onClick={() => setOpen((v) => !v)}>
        <span className="text-xs font-semibold">⚑ Trigger-word watchlist — teach the engine new vocabulary</span>
        <span className="badge">{lx ? `${lx.active_terms} active` : "…"}</span>
        {totalHits > 0 && <span className="badge sev-high">{totalHits} corpus hits</span>}
        <span className="muted text-[11px] ml-auto">{open ? "▲ collapse" : "▼ expand"}</span>
      </button>
      <div className="muted text-[11px]" style={{ marginTop: 4 }}>
        Built-in lexicon: {lx?.built_in.total_terms ?? "—"} curated terms in {Object.keys(lx?.built_in.categories ?? {}).length} categories.
        Add terrorist/offender slang the matrix doesn't know yet — Arabic, English or Arabizi, single words or phrases.
        Changes re-score the whole corpus automatically and are audited.
      </div>

      {open && (
        <div className="flex flex-col gap-3" style={{ marginTop: 12 }}>
          <div className="flex gap-2 flex-wrap items-end">
            <div className="flex flex-col gap-1" style={{ minWidth: 240, flex: 1 }}>
              <span className="muted text-[10px] uppercase">New trigger word / phrase</span>
              <input className="field" placeholder="e.g. qusos alkhilafa · خريجي الجماعه" value={term}
                onChange={(e) => setTerm(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && !busy && add()} dir="auto" />
            </div>
            <div className="flex flex-col gap-1">
              <span className="muted text-[10px] uppercase">Severity</span>
              <select className="field" value={sev} onChange={(e) => setSev(e.target.value)}
                title={SEV_TIP[sev]}>
                <option value="watch">watch — trend only</option>
                <option value="flag">flag — human review queue</option>
                <option value="severe">severe — auto-alert band</option>
              </select>
            </div>
            <div className="flex flex-col gap-1" style={{ flex: 1, minWidth: 160 }}>
              <span className="muted text-[10px] uppercase">Why (note, kept in audit)</span>
              <input className="field" placeholder="e.g. new alias observed in cluster C1" value={note}
                onChange={(e) => setNote(e.target.value)} />
            </div>
            <button className="btn btn-primary" disabled={busy || !term.trim()} onClick={add}>Add term</button>
          </div>

          <div className="flex gap-2 items-end">
            <div className="flex flex-col gap-1" style={{ maxWidth: 320, flex: 1 }}>
              <span className="muted text-[10px] uppercase">Is a word already covered? search built-in + watchlist</span>
              <input className="field" placeholder="substring, e.g. khilafa" value={q}
                onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} dir="auto" />
            </div>
            <button className="btn" disabled={busy || !q.trim()} onClick={search}>Search</button>
            {found && (
              <div className="text-[11px] flex flex-wrap gap-1 items-center" style={{ maxWidth: 520 }}>
                {found.built_in.length === 0 && found.watchlist.length === 0 &&
                  <span className="muted">not covered anywhere — safe to add</span>}
                {found.built_in.slice(0, 8).map((b) => (
                  <span key={b.term} className="badge" title={`${b.category} · w ${b.weight}`}>{b.term}</span>))}
                {found.watchlist.map((w) => (
                  <span key={w.id} className="badge" style={{ borderColor: "rgba(53,224,200,.5)" }}>⚑ {w.term}</span>))}
              </div>)}
          </div>

          {msg && <div className="text-[11px]" style={{ color: "var(--accent)" }}>{msg}</div>}

          {(lx?.custom.length ?? 0) > 0 ? (
            <div className="flex flex-col gap-1.5">
              {lx!.custom.map((t) => (
                <div key={t.id} className="flex items-center gap-2 text-xs flex-wrap"
                  style={{ opacity: t.enabled ? 1 : 0.45, borderTop: "1px solid var(--line)", paddingTop: 6 }}>
                  <span className="mono" style={{ fontWeight: 600 }} dir="auto">{t.term}</span>
                  <span className={`badge ${t.severity === "severe" ? "sev-high" : t.severity === "flag" ? "sev-moderate" : ""}`}>{t.severity}</span>
                  <span className="muted mono text-[10px]">w {t.weight}</span>
                  <span className={`badge ${t.hits ? "badge-live" : ""}`}>{t.hits} hits</span>
                  {t.note && <span className="muted text-[11px] truncate" style={{ maxWidth: 260 }} title={t.note}>{t.note}</span>}
                  <div className="ml-auto flex gap-1.5">
                    <button className="btn" style={{ padding: "1px 8px", fontSize: 11 }} disabled={busy}
                      onClick={() => toggle(t)}>{t.enabled ? "disable" : "enable"}</button>
                    <button className="btn" style={{ padding: "1px 8px", fontSize: 11 }} disabled={busy}
                      onClick={() => remove(t)}>remove</button>
                  </div>
                </div>))}
              <div className="muted text-[10px]">Every add/change queues a corpus-wide re-score; matching posts surface here
                in the feed and as watchlist alerts. Terms match with the same normalization, transliteration and
                typo-tolerance as the curated matrix.</div>
            </div>
          ) : (
            <div className="muted text-[11px]">No custom terms yet — the matrix above is running as designed.</div>
          )}
        </div>
      )}
    </div>
  );
}

export default function ThreatsPage() {
  const [feed, setFeed] = useState<RiskFeed | null>(null);
  const [ov, setOv] = useState<Overview | null>(null);
  const [sev, setSev] = useState("");
  const [cat, setCat] = useState("");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const load = () => {
      const qs = new URLSearchParams({ ...(sev && { severity: sev }), ...(cat && { category: cat }) }).toString();
      api.get<RiskFeed>(`/api/v1/analysis/risk${qs ? `?${qs}` : ""}`)
        .then(setFeed).catch(() => setFeed(null));
      api.get<Overview>("/api/v1/overview").then(setOv).catch(() => {});
    };
    load();
    const t = setInterval(load, 6000);
    return () => clearInterval(t);
  }, [sev, cat, tick]);

  const risk = ov?.risk;
  const cats = Object.entries(risk?.by_category ?? {}).sort((a, b) => b[1] - a[1]);
  const maxCat = Math.max(1, ...cats.map(([, n]) => n));

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-lg font-semibold">Threat Monitor — Bilingual Content Risk</h1>
          <div className="muted text-xs">Arabic + English triage classifier · every item links to stored evidence</div>
        </div>
        {risk && (
          <div className={`card glass px-4 py-2 flex items-center gap-3 ${THREAT_GLOW[risk.threat_level] ?? ""}`}>
            <span className="pulse" style={{ width: 9, height: 9, borderRadius: 9,
              background: risk.threat_level === "low" ? "#35e0c8" : risk.threat_level === "moderate" ? "#ffb24d" : "#ff4d6d",
              display: "inline-block" }} />
            <div>
              <div className="text-[10px] muted uppercase" style={{ letterSpacing: ".08em" }}>Threat level</div>
              <div className="text-sm font-semibold" style={{ textTransform: "capitalize" }}>{risk.threat_level}</div>
            </div>
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="card glass"><div className="muted text-[11px] uppercase">Flagged posts</div>
          <div className="text-2xl font-semibold">{risk?.flagged ?? "—"}</div></div>
        <div className="card glass"><div className="muted text-[11px] uppercase">High severity</div>
          <div className="text-2xl font-semibold sev-high">{risk?.high_severity ?? "—"}</div></div>
        <div className="card glass" title="every collected post is risk-scored automatically">
          <div className="muted text-[11px] uppercase">Posts analyzed</div>
          <div className="text-2xl font-semibold">{Object.values(ov?.posts_by_origin ?? {}).reduce((a, b) => a + (Number(b) || 0), 0)}</div></div>
        <div className="card glass"><div className="muted text-[11px] uppercase">Engine</div>
          <div className="text-sm font-semibold" style={{ marginTop: 6 }}>lexicon + centroid <span className="badge">mvp</span></div>
          <div className="muted text-[10px]">fine-tuned multilingual model behind the same interface later</div></div>
      </div>

      <Watchlist bump={() => setTick((t) => t + 1)} />

      {cats.length > 0 && (
        <div className="card glass">
          <div className="text-xs font-semibold" style={{ marginBottom: 10 }}>Detections by category</div>
          <div className="flex flex-col gap-2">
            {cats.map(([c, n]) => (
              <button key={c} onClick={() => { setCat(cat === c ? "" : c); setTick(t => t + 1); }}
                className="text-left" style={{ display: "grid", gridTemplateColumns: "180px 1fr 40px",
                gap: 10, alignItems: "center", cursor: "pointer", opacity: cat && cat !== c ? 0.45 : 1 }}>
                <span className="text-xs">{CAT_LABELS[c] ?? c}{cat === c ? " ✕" : ""}</span>
                <span style={{ height: 8, borderRadius: 6, background: "rgba(255,255,255,0.06)", display: "block" }}>
                  <span style={{ display: "block", height: "100%", borderRadius: 6, width: `${(n / maxCat) * 100}%`,
                    background: c === "benign" || c === "counterspeech" ? "rgba(53,224,200,.55)"
                      : c === "hate_speech" || c === "racism" ? "rgba(255,178,77,.75)" : "rgba(255,77,109,.8)" }} />
                </span>
                <span className="mono text-xs" style={{ textAlign: "right" }}>{n}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        {["", "high", "moderate"].map((sv) => (
          <button key={sv || "all"} className="badge" onClick={() => { setSev(sv); setTick(t => t + 1); }}
            style={{ borderColor: sev === sv ? "rgba(53,224,200,.6)" : undefined,
                     background: sev === sv ? "rgba(53,224,200,.12)" : undefined }}>
            {sv === "" ? "all severities" : sv}
          </button>
        ))}
        {cat && <button className="badge" onClick={() => { setCat(""); setTick(t => t + 1); }}>category: {CAT_LABELS[cat] ?? cat} ✕</button>}
        <span className="muted text-[11px]" style={{ marginLeft: "auto" }}>auto-refresh 6s · {feed?.count ?? 0} items</span>
      </div>

      <div className="flex flex-col gap-2">
        {(feed?.items ?? []).map((it) => (
          <div key={it.post_id} className={`card card-hover glass ${it.severity === "high" ? "glow-crimson" : ""}`}
            style={{ padding: "10px 14px" }}>
            <div className="flex items-center gap-2 flex-wrap" style={{ marginBottom: 4 }}>
              <span className={`badge ${it.severity === "high" ? "sev-high" : "sev-moderate"}`}
                style={{ textTransform: "uppercase", fontSize: 9 }}>{it.severity}</span>
              <span className="badge mono">{it.language.toUpperCase()}</span>
              <span className="text-xs font-semibold">{CAT_LABELS[it.category] ?? it.category}</span>
              <span className="muted text-[11px] mono">score {it.score.toFixed(0)} · conf {typeof it.confidence === "number" ? it.confidence.toFixed(1) : it.confidence}</span>
              {it.human_review_required && <span className="badge sev-high">REVIEW REQUIRED</span>}
              {it.moderation?.action && (
                <span className="badge mono" style={{
                  fontSize: 9,
                  color: it.moderation.action === "ACTION_BLOCK" ? "#ff4d6d"
                    : it.moderation.action === "ACTION_FLAG_HUMAN_REVIEW" ? "#ffb24d" : "#8aa0b4",
                  borderColor: "currentColor",
                }}>{it.moderation.action.replace("ACTION_", "")}</span>
              )}
              {it.moderation && (it.moderation.scores.extremism_threat_score > 0 || it.moderation.scores.profanity_harassment_score > 0) && (
                <span className="muted text-[10px] mono" title="extremism-threat / profanity-harassment axis scores">
                  T{it.moderation.scores.extremism_threat_score.toFixed(2)} P{it.moderation.scores.profanity_harassment_score.toFixed(2)}
                </span>
              )}
              <span className="badge">{it.origin_mode}</span>
              <span className="flex items-center gap-2" style={{ marginLeft: "auto" }}>
                <Link className="btn btn-primary" style={{ fontSize: 10, padding: "2px 10px" }}
                  title="open in Investigations: adds this post + account to the human-review queue for extraction, analysis and reporting"
                  href={`/cases?review=1&post=${encodeURIComponent(it.post_id)}&account=${encodeURIComponent(it.author_id)}&handle=${encodeURIComponent(it.author_handle ?? "")}&cat=${encodeURIComponent(it.category)}&score=${it.score.toFixed(0)}&sev=${encodeURIComponent(it.severity)}`}>investigate →</Link>
                <Link className="muted text-[11px]" title="open account dossier"
                  href={`/accounts?id=${encodeURIComponent(it.author_id)}`}>@{it.author_handle ?? it.author_id} →</Link>
              </span>
            </div>
            <div dir={it.language.startsWith("ar") ? "rtl" : "ltr"} className="text-sm"
              style={{ wordBreak: "break-word" }}>{it.excerpt}</div>
            <div className="muted text-[10px]" style={{ marginTop: 4, wordBreak: "break-word" }}>
              matched: {[...new Set([...(it.matched_terms || []), ...(it.moderation?.matched_terms || [])])].join(", ") || "—"}
              {" · "}{it.moderation?.reasoning || it.caveats[0] || ""}
            </div>
          </div>
        ))}
        {feed && feed.items.length === 0 && (
          <div className="card glass muted text-sm" style={{ padding: 20 }}>No posts in this filter — the classifier
            flags only when bilingual lexicon/centroid evidence crosses threshold. Absence of flags is a finding, not a bug.</div>
        )}
      </div>

      {feed?.note && <div className="muted text-[11px]" style={{ maxWidth: 860 }}>{feed.note}</div>}
    </div>
  );
}
