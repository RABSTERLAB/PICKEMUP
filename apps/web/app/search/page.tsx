"use client";

/* Investigation Search — one query over the local corpus, grouped like a
 * triage board (entities / accounts / posts), then pivot: every result can
 * run typed local transforms over a typed entity index.
 * Results carry evidence lineage back to the stored posts that produced them. */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";

type Ent = { etype: string; value: string; label: string; post_count: number;
  first_seen: string | null; last_seen: string | null };
type Acct = { id: string; username: string; display_name: string; origin_mode: string;
  automation_verdict: string | null };
type PostHit = { id: string; author_id: string; author: string; excerpt: string;
  created_at: string | null; lang: string | null; origin_mode: string };
type SearchRes = { query: string; entities: Ent[]; accounts: Acct[]; posts: PostHit[];
  watchlist_hits: string[]; totals: Record<string, number>; note: string };
type Tf = { name: string; display: string; set: string; inputs: string[]; output: string;
  description: string };
type TfRes = { transform: string; output_type: string; results: { value: string; label: string;
  weight: number; evidence: string[] }[]; note?: string };
type Stats = { types: Record<string, { entities: number; occurrences: number }>; entities_total: number };

const ETYPE_ICON: Record<string, string> = { HASHTAG: "#", MENTION: "@", DOMAIN: "🌐", URL: "🔗", EMAIL: "✉", PHONE: "☎" };
const ETYPE_COLOR: Record<string, string> = {
  HASHTAG: "#35e0c8", MENTION: "#7aa2ff", DOMAIN: "#f0a35e", URL: "#9b8cff", EMAIL: "#e07ab0", PHONE: "#6ad07a",
};

function PivotPanel({ etype, value, transforms, runPivot, out }: {
  etype: string; value: string; transforms: Tf[];
  runPivot: (name: string) => void; out: Record<string, TfRes>;
}) {
  const tfs = transforms.filter((t) => t.inputs.includes(etype));
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: 6 }}>
      <button className="btn btn-ghost !py-0.5 !px-2 text-[11px]" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} pivots ({tfs.length})
      </button>
      {open && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {tfs.map((t) => {
            const res = out[`${etype}:${value}:${t.name}`];
            return (
              <span key={t.name} className="badge" title={t.description}
                style={{ cursor: "pointer", borderColor: res ? "rgba(53,224,200,.45)" : undefined }}
                onClick={() => runPivot(t.name)}>
                {t.display} → {t.output}
                {res ? ` (${res.results.length})` : ""}
              </span>
            );
          })}
        </div>
      )}
      {open && tfs.map((t) => {
        const res = out[`${etype}:${value}:${t.name}`];
        if (!res) return null;
        return (
          <div key={t.name + "-out"} className="mt-1.5 rounded-lg border"
            style={{ borderColor: "var(--line)", background: "var(--panel)", padding: "6px 10px" }}>
            <div className="text-[11px] opacity-70">{t.display} — {t.output === "POST" ? "posts" : "results"}:</div>
            {res.results.length === 0
              ? <div className="text-[12px] opacity-60">{res.note || "no local evidence"}</div>
              : <ul className="text-[12px] leading-5">
                  {res.results.slice(0, 8).map((r) => (
                    <li key={r.value} className="flex gap-2 items-baseline">
                      <span dir="auto" className="font-medium">{r.value}</span>
                      {r.label && r.label !== r.value && <span className="opacity-60 truncate max-w-[220px]">{r.label}</span>}
                      <span className="opacity-50">×{r.weight}</span>
                      {r.evidence?.length > 0 && (
                        <span className="opacity-40 text-[10px]" title={r.evidence.join("\n")}>
                          {r.evidence.length} evid
                        </span>)}
                    </li>
                  ))}
                  {res.results.length > 8 && <li className="opacity-50">…{res.results.length - 8} more</li>}
                </ul>}
          </div>
        );
      })}
    </div>
  );
}

export default function SearchPage() {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<SearchRes | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [tfs, setTfs] = useState<Tf[]>([]);
  const [out, setOut] = useState<Record<string, TfRes>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.get<Stats>("/api/v1/search/stats").then(setStats).catch(() => setStats(null));
    api.get<{ transforms: Tf[] }>("/api/v1/transforms").then((d) => setTfs(d.transforms)).catch(() => setTfs([]));
  }, []);

  const search = useCallback(async (query?: string) => {
    const term = (query ?? q).trim();
    if (!term) return;
    setBusy(true); setErr(null);
    try {
      setRes(await api.get<SearchRes>(`/api/v1/search?q=${encodeURIComponent(term)}`));
    } catch (e) { setErr(String((e as Error)?.message || e)); }
    finally { setBusy(false); }
  }, [q]);

  const runPivot = useCallback(async (etype: string, value: string, name: string) => {
    const key = `${etype}:${value}:${name}`;
    if (out[key]) return;
    try {
      const r = await api.post<TfRes>("/api/v1/transform",
        { input_type: etype, value, name });
      setOut((o) => ({ ...o, [key]: r }));
    } catch { setOut((o) => ({ ...o, [key]: { transform: name, output_type: "?", results: [], note: "pivot failed" } })); }
  }, [out]);

  const grouped = useMemo(() => {
    const g: Record<string, Ent[]> = {};
    for (const e of res?.entities ?? []) (g[e.etype] ||= []).push(e);
    return g;
  }, [res]);

  return (
    <div className="mx-auto max-w-[1080px] px-5 py-6">
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-lg font-semibold">Investigation Search</h1>
          <p className="text-[12px] opacity-60">One query → typed entities, accounts, posts — pivot locally, every result tied to stored evidence.</p>
        </div>
        {stats && (
          <div className="flex gap-1.5 flex-wrap">
            {Object.entries(stats.types).map(([t, v]) => (
              <span key={t} className="badge" style={{ borderColor: `${ETYPE_COLOR[t]}44`, color: ETYPE_COLOR[t] }}>
                {ETYPE_ICON[t]} {t} {v.entities}
              </span>
            ))}
          </div>
        )}
      </div>

      <form className="mt-4 flex gap-2" onSubmit={(e) => { e.preventDefault(); search(); }}>
        <input className="input flex-1" placeholder="hashtag, handle, domain, email, phrase…  (e.g. syntheticrally)"
          value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
        <button className="btn btn-primary" disabled={busy}>{busy ? "Searching…" : "Search"}</button>
      </form>
      {err && <div className="mt-2 text-[12px]" style={{ color: "#ff7a90" }}>{err}</div>}

      {!res && !busy && (
        <div className="mt-6 card p-4 text-[12px] opacity-70 leading-5">
          <b>How to read this page.</b> The index is built from every stored post — hashtags (incl. Arabic),
          mentions, links, domains, emails, phones. Start from any typed result and run a <i>pivot</i>:
          “Posts containing this”, “Who posted this”, “Co-occurring hashtags/domains”… Each pivot reports how
          many local evidence records back it, and results without lineage are never shown.
          {stats && <> Currently indexing <b>{stats.entities_total}</b> distinct entities.</>}
        </div>
      )}

      {res && (
        <div className="mt-5 grid gap-4 lg:grid-cols-[1.15fr_1fr]">
          {/* entities column */}
          <div className="card p-4">
            <div className="text-[13px] font-semibold">Entities
              <span className="opacity-50 font-normal"> — {res.totals.matched_entities} matched</span></div>
            {Object.keys(grouped).length === 0 && <div className="text-[12px] opacity-50 mt-2">none</div>}
            {Object.entries(grouped).map(([t, ents]) => (
              <div key={t} className="mt-3">
                <div className="text-[11px] uppercase tracking-wider" style={{ color: ETYPE_COLOR[t] }}>
                  {ETYPE_ICON[t]} {t}s</div>
                <div className="mt-1.5 flex flex-col gap-2">
                  {ents.slice(0, 8).map((e) => (
                    <div key={e.value} className="rounded-lg border px-3 py-2"
                      style={{ borderColor: "var(--line)", background: "var(--panel)" }}>
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-[13px] font-medium" dir="auto">{ETYPE_ICON[t]}{e.label}</span>
                        <span className="badge">×{e.post_count} posts</span>
                        {e.last_seen && <span className="text-[10px] opacity-45">seen {e.last_seen.slice(0, 10)}</span>}
                        {res.watchlist_hits.includes(e.value.replace(/^[#@]/, "")) && (
                          <span className="badge" style={{ borderColor: "rgba(255,122,144,.5)", color: "#ff7a90" }}>⚑ watched</span>
                        )}
                      </div>
                      <PivotPanel etype={t} value={e.value} transforms={tfs} out={out}
                        runPivot={(name) => runPivot(t, e.value, name)} />
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>

          {/* accounts + posts column */}
          <div className="flex flex-col gap-4">
            <div className="card p-4">
              <div className="text-[13px] font-semibold">Accounts
                <span className="opacity-50 font-normal"> — {res.totals.matched_accounts}</span></div>
              <ul className="mt-2 flex flex-col gap-1.5">
                {res.accounts.map((a) => (
                  <li key={a.id} className="text-[13px]">
                    <div className="flex items-center gap-2 flex-wrap">
                      <Link className="hover:underline" href={`/accounts?id=${a.id}`} dir="auto">@{a.username}</Link>
                      {a.automation_verdict && a.automation_verdict !== "human_likely" && (
                        <span className="badge" style={{ borderColor: "rgba(255,122,144,.45)", color: "#ff7a90" }}>{a.automation_verdict}</span>
                      )}
                      {a.origin_mode !== "live" && <span className="badge opacity-60">{a.origin_mode}</span>}
                    </div>
                    <PivotPanel etype="ACCOUNT" value={a.username} transforms={tfs} out={out}
                      runPivot={(name) => runPivot("ACCOUNT", a.username, name)} />
                  </li>
                ))}
              </ul>
              {res.accounts.length === 0 && <div className="text-[12px] opacity-50 mt-2">none</div>}
            </div>

            <div className="card p-4">
              <div className="text-[13px] font-semibold">Latest posts
                <span className="opacity-50 font-normal"> — {res.totals.matched_posts}</span></div>
              {res.posts.length === 0 && <div className="text-[12px] opacity-50 mt-2">none</div>}
              <ul className="mt-2 flex flex-col gap-2.5">
                {res.posts.map((p) => (
                  <li key={p.id} className="rounded-lg border px-3 py-2 text-[12px] leading-5"
                    style={{ borderColor: "var(--line)", background: "var(--panel)" }}>
                    <div className="flex gap-2 items-baseline flex-wrap">
                      <Link className="font-medium hover:underline" href={`/accounts?id=${p.author_id}`} dir="auto">@{p.author}</Link>
                      {p.created_at && <span className="opacity-45 text-[10px]">{p.created_at.slice(0, 16).replace("T", " ")}</span>}
                      {p.lang && <span className="opacity-40 text-[10px]">{p.lang}</span>}
                    </div>
                    <div dir="auto" className="mt-0.5 opacity-85">{p.excerpt}</div>
                  </li>
                ))}
              </ul>
            </div>

            {res.watchlist_hits.length > 0 && (
              <div className="card p-3 text-[12px]" style={{ borderColor: "rgba(255,122,144,.4)" }}>
                ⚑ This query matches active watchlist terms:{" "}
                {res.watchlist_hits.map((t) => <span key={t} className="badge mr-1" dir="auto">{t}</span>)}
                <Link className="ml-1 underline opacity-70" href="/threats">manage in Threat Monitor</Link>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="mt-6 text-[11px] opacity-40">
        Local-corpus search & typed transforms over the entity index. No external provider calls.
      </div>
    </div>
  );
}
