"use client";
import { useEffect, useState } from "react";
import { api, type Cluster } from "@/lib/api";
import { VerdictBadge } from "../shell";

export default function CampaignsPage() {
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [err, setErr] = useState("");
  const [caseId, setCaseId] = useState("");
  const [added, setAdded] = useState<string[]>([]);

  useEffect(() => {
    api.get<{ clusters: Cluster[] }>("/api/v1/analysis/coordination")
      .then((r) => setClusters(r.clusters)).catch((e) => setErr(String(e)));
  }, []);

  async function addCase(c: Cluster) {
    let cid = caseId;
    if (!cid) {
      const r = await api.post<{ case_id: string }>("/api/v1/cases",
        { title: `Cluster ${c.cluster_id} investigation`, scope: "Created from coordination cluster" });
      cid = r.case_id; setCaseId(cid);
    }
    await api.post(`/api/v1/cases/${cid}/members`, { kind: "cluster", ref_id: c.cluster_id });
    setAdded((a) => (a.includes(`${c.cluster_id} → ${cid}`) ? a : [...a, `${c.cluster_id} → ${cid}`]));  // stable keys
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Coordination Clusters</h1>
      <p className="muted text-sm max-w-3xl">Clusters group accounts connected by observed behavioral signals.
        Verdicts state <b>possibility</b>, never proof of control; alternative organic explanations are listed when plausible.</p>
      {err && <div className="card p-3 text-sm" style={{ color: "var(--danger)" }}>{err}</div>}
      {added.map((a) => <div key={a} className="card p-2 text-xs" style={{ color: "var(--accent)" }}>added {a}</div>)}
      <div className="grid lg:grid-cols-2 gap-4">
        {clusters.map((c) => (
          <div key={c.cluster_id} className="card p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="mono text-sm font-semibold">{c.cluster_id}</span>
                <VerdictBadge v={c.verdict} />
              </div>
              <button className="btn" onClick={() => addCase(c)}>add to case</button>
            </div>
            <div className="grid grid-cols-3 gap-2 text-center">
              <div className="card p-2"><div className="text-lg tabular-nums">{c.accounts}</div><div className="kicker">accounts</div></div>
              <div className="card p-2"><div className="text-lg tabular-nums">{c.edge_count}</div><div className="kicker">edges</div></div>
              <div className="card p-2"><div className="text-lg tabular-nums">{(c.confidence * 100).toFixed(0)}%</div><div className="kicker">confidence</div></div>
            </div>
            <div>
              <div className="kicker mb-1.5">Signal mix (edges)</div>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(c.signal_mix).map(([k, n]) => (
                  <span key={k} className="badge">{k.replaceAll("_", " ")} ×{n}</span>))}
              </div>
            </div>
            {c.amplifiers.length > 0 && (
              <div><span className="kicker">Amplifiers </span>
                <span className="mono text-xs">{c.amplifiers.slice(0, 4).join(", ")}</span></div>)}
            {c.time_range && <div className="text-[11px] muted">activity {c.time_range[0].slice(0, 16)} → {c.time_range[1].slice(0, 16)} UTC</div>}
            {c.organic_explanations.length > 0 && (
              <div className="text-xs" style={{ color: "var(--warn)" }}>
                {c.organic_explanations.map((o, i) => <div key={i}>◌ {o}</div>)}
              </div>)}
            {c.caveats.map((cv, i) => <div key={i} className="text-[11px] muted">⚠ {cv}</div>)}
            <details className="text-xs">
              <summary className="muted cursor-pointer">members ({c.accounts})</summary>
              <div className="mono muted mt-2 leading-relaxed break-all">{c.members.join(" · ")}</div>
            </details>
          </div>
        ))}
        {clusters.length === 0 && <div className="card p-8 muted text-sm">No clusters stored — load data and run “analyze coordination”.</div>}
      </div>
    </div>
  );
}
