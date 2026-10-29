"use client";
import { useEffect, useState } from "react";
import { api, type Narrative } from "@/lib/api";

export default function NarrativesPage() {
  const [items, setItems] = useState<Narrative[]>([]);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [err, setErr] = useState("");

  const load = () => api.get<Narrative[]>("/api/v1/analysis/narratives").then(setItems).catch((e) => setErr(String(e)));
  useEffect(() => { load(); }, []);

  async function run() {
    if (!q.trim()) return;
    setStatus("queued analysis job…");
    const j = await api.post<{ id: number }>("/api/v1/analysis/narratives", { query: q });
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      const job = await api.get<{ status: string; error: string | null }>(`/api/v1/jobs/${j.id}`);
      if (job.status === "done") { setStatus(""); await load(); setOpen(null); return; }
      if (job.status === "dead") { setErr(`job failed: ${job.error}`); setStatus(""); return; }
    }
    setStatus("job still running — refresh shortly");
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Narrative Origins</h1>
      <p className="muted text-sm max-w-3xl">Earliest <b>observed</b> instances and propagation structure for a
        phrase, hashtag, or URL. Coverage gaps and the observation-vs-origin distinction are always disclosed.</p>
      <div className="flex gap-2 max-w-xl">
        <input className="field" placeholder="e.g. synthetic-rally, #NewChildrensHospital, example.org…" value={q}
               onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && run()} />
        <button className="btn btn-primary" onClick={run}>Trace</button>
      </div>
      {status && <div className="card p-2 text-xs" style={{ color: "var(--accent)" }}>{status}</div>}
      {err && <div className="card p-2 text-xs" style={{ color: "var(--danger)" }}>{err}</div>}
      <div className="space-y-3">
        {items.map((n) => (
          <div key={n.narrative_id} className="card p-4 space-y-3">
            <div className="flex items-center justify-between cursor-pointer" onClick={() => setOpen(open === n.narrative_id ? null : n.narrative_id)}>
              <div className="flex items-center gap-3">
                <span className="mono text-sm">{n.narrative_id}</span>
                <span className="badge">query: {n.query}</span>
                <span className="muted text-xs">{n.post_count} posts · algo {n.algo_version}</span>
              </div>
              <span className="muted text-xs">{open === n.narrative_id ? "collapse" : "expand timeline"}</span>
            </div>
            {n.first_observed && (
              <div className="card p-3" style={{ borderColor: "rgba(53,224,200,.3)" }}>
                <div className="kicker" style={{ color: "var(--accent)" }}>earliest OBSERVED instance</div>
                <div className="text-sm mt-1">“{n.first_observed.text.slice(0, 180)}”</div>
                <div className="mono muted text-[11px] mt-1">{n.first_observed.author_id} · {n.first_observed.at} · {n.first_observed.post_id}</div>
              </div>)}
            {open === n.narrative_id && (
              <>
                <div className="max-h-72 overflow-y-auto pr-2">
                  {n.cascade.slice(0, 200).map((c, i) => (
                    <div key={c.post_id + i} className="flex gap-3 items-baseline py-1 border-b border-[var(--line)] text-xs">
                      <span className="mono muted w-36 shrink-0">{c.at?.slice(0, 19)}</span>
                      <span className={`badge ${c.kind === "repost" ? "badge-warn" : "badge-live"}`}>{c.kind}</span>
                      <span className="mono">{c.author_id}</span>
                      <span className="muted mono ml-auto">{c.post_id}</span>
                    </div>))}
                </div>
                {n.amplifiers.length > 0 && <div><span className="kicker">Amplified targets </span>
                  <span className="mono text-xs">{n.amplifiers.join(", ")}</span></div>}
                <div className="flex flex-wrap gap-2">
                  {n.domains.map(d => <span key={d} className="badge badge-mock">{d}</span>)}
                  {n.hashtags.map(h => <span key={h} className="badge">#{h}</span>)}
                </div>
              </>)}
            <div className="space-y-1">
              {n.coverage_gaps.map((g, i) => <div key={i} className="text-[11px]" style={{ color: "var(--warn)" }}>◌ {g}</div>)}
              {n.caveats.map((c, i) => <div key={i} className="text-[11px] muted">⚠ {c}</div>)}
            </div>
          </div>
        ))}
        {items.length === 0 && <div className="card p-8 muted text-sm">No narratives yet — trace a phrase above.</div>}
      </div>
    </div>
  );
}
