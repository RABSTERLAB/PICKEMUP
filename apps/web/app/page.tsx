"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, type Overview, type RiskFeed, type RiskItem, type AccountRow } from "@/lib/api";
import { VerdictBadge } from "./shell";

type XCon = { token_set: boolean; token_source: "settings" | "env" | "none"; token_mask: string | null;
  per_job_usd: number; per_day_usd: number; spend_today_usd: number };
type Job = { id: number; kind: string; status: string; result?: Record<string, unknown> | null; error?: string | null };

function Stat({ label, value, sub, href }: { label: string; value: string | number; sub?: string; href?: string }) {
  const inner = (<><div className="kicker">{label}{href && <span className="ml-1 opacity-60">→</span>}</div>
      <div className="text-2xl font-semibold mt-1.5 tabular-nums">{value}</div>
      {sub && <div className="muted text-[11px] mt-1">{sub}</div>}</>);
  if (href) return (
    <Link href={href} className="card p-4 block hover:border-[var(--accent)] transition-colors" title={`open ${href}`}>{inner}</Link>);
  return (
    <div className="card p-4">{inner}</div>
  );
}

function CollectX({ xc, onDone }: { xc: XCon | null; onDone: () => void }) {
  const [src, setSrc] = useState<"bluesky" | "x_api">("bluesky");
  const [query, setQuery] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);

  async function collect() {
    if (!query.trim() || busy) return;
    setBusy(true); setErr(""); setJob(null);
    try {
      const j = await api.post<Job>("/api/v1/jobs", { kind: src, params: { query: query.trim() } });
      setJob(j);
      poll.current = setInterval(async () => {
        try {
          const cur = await api.get<Job>(`/api/v1/jobs/${j.id}`);
          setJob(cur);
          if (cur.status === "done" || cur.status === "dead") {
            if (poll.current) clearInterval(poll.current);
            setBusy(false);
            onDone();
          }
        } catch { /* transient poll error — keep polling */ }
      }, 2500);
    } catch (e) { setErr(String(e)); setBusy(false); }
  }

  const res = job?.result as { posts_new?: number; posts_deduped?: number; spend_usd?: number; stopped_reason?: string } | undefined;
  return (
    <div className="card p-4 space-y-2">
      <div className="flex items-center justify-between">
        <span className="kicker">Collect live — from {src === "bluesky" ? "Bluesky" : "X"}</span>
        <span className="muted text-[11px]">
          {src === "bluesky"
            ? "free public API · no key required"
            : `cap $${xc!.per_job_usd}/job · $${xc!.per_day_usd}/day · spent today $${xc!.spend_today_usd.toFixed(3)}`}
        </span>
      </div>
      <div className="flex gap-2">
        <select className="field" style={{ width: 140, flex: "none" }} value={src}
          onChange={(e) => setSrc(e.target.value as "bluesky" | "x_api")} disabled={busy}>
          <option value="bluesky">Bluesky · free</option>
          <option value="x_api">X · paid</option>
        </select>
        <input className="field flex-1"
          placeholder={src === "bluesky"
            ? "search query, e.g. \"coordinated inauthentic behavior\" or a keyword/phrase"
            : "X recent-search query, e.g. (khilafa OR خلاففة) lang:ar -is:retweet"}
          value={query} onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && collect()} disabled={busy} />
        <button className="btn btn-primary" onClick={collect}
          disabled={busy || !query.trim() || (src === "x_api" && !xc?.token_set)}>
          {busy ? "collecting…" : "start collection"}
        </button>
      </div>
      {src === "x_api" && !xc?.token_set && (
        <div className="text-xs" style={{ color: "var(--warn)" }}>No X bearer token configured —
          add one on Data Sources, or collect from Bluesky (free) instead.</div>
      )}
      <div className="text-[11px] muted">
        X spend caps are enforced BEFORE any HTTP call · every ingested post carries provenance ·
        the full engine chain (automation · risk · coordination · narratives) runs automatically after ingest.
      </div>
      {job && (
        <div className="text-xs" style={{ color: job.status === "dead" ? "var(--danger)" : "var(--accent)" }}>
          {job.status === "queued" || job.status === "running"
            ? `job #${job.id} ${job.status} — contacting X API…`
            : job.status === "done"
              ? `✓ done — ${res?.posts_new ?? 0} new posts (${res?.posts_deduped ?? 0} dupes), spend $${(res?.spend_usd ?? 0).toFixed(4)}`
                + (res?.stopped_reason ? ` · stopped early: ${res.stopped_reason}` : "")
                + " · analysis chain queued"
              : `⚠ ${job.error || "job failed"}`}
        </div>
      )}
      {err && <div className="text-xs" style={{ color: "var(--danger)" }}>{err}</div>}
    </div>
  );
}

export default function OverviewPage() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [note, setNote] = useState("");
  const [xc, setXc] = useState<XCon | null>(null);
  const [devTools, setDevTools] = useState(false);

  const refresh = useCallback(() => api.get<Overview>("/api/v1/overview").then(setOv).catch((e) => setErr(String(e))), []);
  useEffect(() => {
    refresh();
    api.get<XCon>("/api/v1/x-connection").then(setXc).catch(() => {});
    api.get<{ dev_tools: boolean }>("/api/v1/sources").then((r) => setDevTools(r.dev_tools)).catch(() => {});
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, [refresh]);

  async function loadDemo() {
    setLoading(true); setNote(""); setErr("");
    try {
      const r = await api.post<{ enqueued: { id: number }[] }>("/api/v1/demo/load", { seed: 1337 });
      setNote(`queued ${r.enqueued.length} jobs — synthetic pipeline running…`);
      for (let i = 0; i < 40; i++) {
        await new Promise((res) => setTimeout(res, 3000));
        await refresh();
      }
      await refresh();
    } catch (e) { setErr(String(e)); }
    setLoading(false);
  }

  const spend = Object.entries(ov?.api_spend_usd_by_day ?? {});
  const postsTotal = Object.values(ov?.posts_by_origin ?? {}).reduce((a, b) => a + b, 0);
  const empty = !ov || postsTotal === 0;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Overview</h1>
          <p className="muted text-sm mt-0.5">Corpus statistics, engine output and collection health — all values from stored records.</p>
        </div>
        <div className="flex gap-2">
          {xc?.token_set
            ? <span className="badge badge-ok">X connected — via {xc.token_source}</span>
            : <Link href="/sources" className="btn btn-primary">Connect X (token + caps)</Link>}
          {devTools && (
            <button className="btn" onClick={loadDemo} disabled={loading}>
              {loading ? "demo pipeline…" : "offline demo (synthetic)"}
            </button>
          )}
        </div>
      </div>
      {err && <div className="card p-3 text-sm" style={{ color: "var(--danger)" }}>{err}</div>}
      {note && <div className="card p-3 text-sm" style={{ color: "var(--accent)" }}>{note}</div>}

      <CollectX xc={xc} onDone={refresh} />

      {empty ? (
        <div className="card p-10 text-center">
          <div className="text-lg font-medium">No data yet — collect your first real corpus</div>
          <p className="muted text-sm mt-2 max-w-md mx-auto">
            Type a query above and press start. <b>Bluesky</b> needs no account or API key — real public
            posts, free. <b>X</b> needs a funded bearer token (set it on <Link href="/sources">Data Sources</Link>).
            Every post ingests with provenance; the automation, risk, coordination and narrative engines
            then run automatically.
            {devTools && <> Or load the labeled <span className="badge badge-synthetic">synthetic</span>
              campaign (&quot;offline demo&quot;, top-right) to exercise the pipeline offline.</>}
          </p>
        </div>
      ) : ov && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            <Stat label="Threats detected" value={ov.risk?.high_severity ?? 0}
                  sub={`level: ${ov.risk?.threat_level ?? "low"} · ${ov.risk?.flagged ?? 0} flagged`} href="/threats" />
            <Stat label="Accounts analyzed" value={Object.values(ov.accounts_by_origin).reduce((a, b) => a + b, 0)}
                  sub={Object.entries(ov.accounts_by_origin).map(([k, v]) => `${k}: ${v}`).join(" · ")} href="/accounts" />
            <Stat label="Posts collected" value={postsTotal} href="/search" />
            <Stat label="Active clusters" value={ov.top_clusters.filter(c => c.accounts >= 5).length}
                  sub="≥5 accounts, engine output" />
            <Stat label="API spend (USD/day)" value={spend.length ? spend.map(([d, v]) => `$${v.toFixed(3)}`).join(" · ") : "$0.000"}
                  sub="budget caps enforced pre-call" href="/sources" />
          </div>

          <Findings />
          <div className="grid lg:grid-cols-2 gap-4">
            <div className="card p-4">
              <div className="kicker mb-3">Top coordination clusters</div>
              {ov.top_clusters.length === 0 ? <div className="muted text-sm">No coordination clusters found in the analyzed corpus. Every collected interaction is still mapped in <Link className="underline" href="/network">Network Explorer</Link>.</div> : (
                <table className="tbl">
                  <thead><tr><th>Cluster</th><th>Accounts</th><th>Verdict</th><th style={{ width: 110 }}>Confidence</th></tr></thead>
                  <tbody>
                    {ov.top_clusters.map((c) => (
                      <tr key={c.id}>
                        <td className="mono">{c.id}</td>
                        <td>{c.accounts}</td>
                        <td><VerdictBadge v={c.verdict} /></td>
                        <td><div className="confbar"><div style={{ width: `${(c.confidence ?? 0) * 100}%` }} /></div>
                            <span className="muted text-[11px]">{(c.confidence ?? 0).toFixed(2)}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div className="card p-4">
              <div className="kicker mb-3">Recent jobs (durable queue)</div>
              <table className="tbl">
                <thead><tr><th>#</th><th>Kind</th><th>Status</th><th>Result / error</th></tr></thead>
                <tbody>
                  {ov.recent_jobs.map((j) => (
                    <tr key={j.id}>
                      <td className="mono">{j.id}</td>
                      <td>{j.kind}</td>
                      <td><span className={`badge ${j.status === "done" ? "badge-ok" : j.status === "dead" || j.status === "failed" ? "badge-danger" : "badge-warn"}`}>{j.status}</span></td>
                      <td className="muted mono" style={{ maxWidth: 340, whiteSpace: "normal", wordBreak: "break-word", fontSize: 11 }}>
                        {j.error ? `⚠ ${j.error}` : JSON.stringify(j.result).slice(0, 150)}
                      </td>
                    </tr>
                  ))}
                  {ov.recent_jobs.length === 0 && <tr><td colSpan={4} className="muted">none</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}


function Findings() {
  const [risk, setRisk] = useState<RiskItem[]>([]);
  const [accts, setAccts] = useState<AccountRow[]>([]);
  useEffect(() => {
    api.get<RiskFeed>("/api/v1/analysis/risk?limit=5").then((r) => setRisk(r.items ?? [])).catch(() => {});
    api.get<AccountRow[]>("/api/v1/accounts?limit=200")
      .then((rs) => setAccts((Array.isArray(rs) ? rs : []).filter((a) => (a.automation_score ?? 0) > 0)
        .sort((a, b) => (b.automation_score ?? 0) - (a.automation_score ?? 0)).slice(0, 5)))
      .catch(() => {});
  }, []);
  return (
    <div className="card p-4">
      <div className="flex items-center gap-3 mb-3">
        <div className="kicker">What we found — the corpus behind the numbers</div>
        <Link className="ml-auto text-[11px]" style={{ color: "var(--accent)" }} href="/threats">threat monitor →</Link>
        <Link className="text-[11px]" style={{ color: "var(--accent)" }} href="/network">network map →</Link>
      </div>
      <div className="grid md:grid-cols-2 gap-5">
        <div>
          <div className="muted text-[10px] uppercase tracking-wider mb-2">Top flagged content</div>
          {risk.length === 0 && <div className="muted text-xs">Nothing flagged by the risk engine yet — collection is clean so far.</div>}
          {risk.slice(0, 4).map((it) => (
            <div key={it.post_id} className="border-l-2 pl-2.5 mb-2.5"
              style={{ borderColor: it.severity === "high" ? "var(--danger)" : "var(--line)" }}>
              <div className="text-xs">
                <Link className="hover:underline" href={`/accounts?id=${encodeURIComponent(it.author_id)}`}>@{it.author_handle ?? it.author_id}</Link>
                {" · "}{it.category.replaceAll("_", " ")} <span className="muted mono">score {it.score.toFixed(0)}</span>
              </div>
              <div className="text-[11px] muted line-clamp-2">“{it.excerpt}”</div>
            </div>))}
        </div>
        <div>
          <div className="muted text-[10px] uppercase tracking-wider mb-2">Account signals (scored above zero)</div>
          {accts.length === 0 && <div className="muted text-xs">No account carries any automation or weak-profile signal yet.</div>}
          {accts.map((a) => (
            <div key={a.account_id} className="flex items-center gap-2 text-xs mb-2">
              <Link className="hover:underline truncate max-w-[240px]" href={`/accounts?id=${encodeURIComponent(a.account_id)}`}>@{a.username}</Link>
              <VerdictBadge v={a.automation_verdict} />
              <span className="muted mono">score {a.automation_score}</span>
            </div>))}
        </div>
      </div>
    </div>
  );
}
