"use client";
import { useEffect, useState } from "react";
import { API, api, type CaseDetail, type CaseRow } from "@/lib/api";

export default function CasesPage() {
  const [rows, setRows] = useState<CaseRow[]>([]);
  const [sel, setSel] = useState<CaseDetail | null>(null);
  const [title, setTitle] = useState(""); const [scope, setScope] = useState("");
  const [note, setNote] = useState(""); const [findings, setFindings] = useState("");
  const [err, setErr] = useState("");

  const load = () => api.get<CaseRow[]>("/api/v1/cases").then(setRows).catch((e) => setErr(String(e)));
  useEffect(() => { load(); }, []);
  // one guarded runner for every mutation: no unhandled rejections, errors visible
  const run = async (fn: () => Promise<void>) => {
    setErr("");
    try { await fn(); } catch (e) { setErr(String(e)); }
  };
  const open = (id: string) => run(async () => {
    const d = await api.get<CaseDetail>(`/api/v1/cases/${encodeURIComponent(id)}`);
    setSel(d); setFindings(d.findings);
  });

  // Call-to-action from Threat Monitor / Alerts: /cases?review=1&post=..&account=..
  // enrolls the flagged item into the durable "Human review queue" case and opens
  // it directly — extraction, findings and report export are one panel away.
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    if (q.get("review") !== "1") return;
    const QUEUE_TITLE = "Human review queue";
    (async () => {
      try {
        const rows = await api.get<CaseRow[]>("/api/v1/cases");
        let cid = rows.find((r) => r.title === QUEUE_TITLE && r.status !== "closed")?.case_id;
        if (!cid) cid = (await api.post<{ case_id: string }>("/api/v1/cases",
          { title: QUEUE_TITLE, scope: "content-review" })).case_id;
        const post = q.get("post") || "", acct = q.get("account") || "";
        const src = q.get("src") || "threats";
        const handle = q.get("handle") || acct || "?";
        if (post) await api.post(`/api/v1/cases/${encodeURIComponent(cid)}/members`, { kind: "post", ref_id: post });
        if (acct) await api.post(`/api/v1/cases/${encodeURIComponent(cid)}/members`, { kind: "account", ref_id: acct });
        const axis = src === "accounts"
          ? `verdict ${q.get("cat") || "?"} · score ${q.get("score") || "?"} · confidence ${q.get("sev") || "?"}`
          : `${q.get("cat") || "risk"} · score ${q.get("score") || "?"} · severity ${q.get("sev") || "?"}`;
        await api.post(`/api/v1/cases/${encodeURIComponent(cid)}/notes`, {
          text: `${src === "accounts" ? "ACCOUNT UNDER INVESTIGATION" : "FLAGGED FOR HUMAN REVIEW"}`
              + ` — @${handle} · ${axis}${post ? ` · post ${post}` : ""} · ${new Date().toISOString()}`,
        });
        window.history.replaceState({}, "", "/cases");  // never re-enroll on refresh
        await load(); await open(cid);
      } catch (e) { setErr(String(e)); }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Investigations</h1>
      <div className="flex gap-2 max-w-2xl">
        <input className="field" placeholder="new case title" value={title} onChange={(e) => setTitle(e.target.value)} />
        <input className="field" placeholder="scope (optional)" value={scope} onChange={(e) => setScope(e.target.value)} />
        <button className="btn btn-primary" disabled={!title.trim()} onClick={() => run(async () => {
          await api.post("/api/v1/cases", { title, scope }); setTitle(""); await load();
        })}>create</button>
      </div>
      {err && <div className="card p-2 text-xs" style={{ color: "var(--danger)" }}>{err}</div>}
      <div className="flex gap-4 items-start">
        <div className="w-80 shrink-0 card">
          <table className="tbl">
            <thead><tr><th>Case</th><th>Status</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.case_id} className="cursor-pointer" onClick={() => open(r.case_id)}>
                <td><div className="font-medium">{r.title}</div><div className="mono muted text-[11px]">{r.case_id}</div></td>
                <td><span className={`badge ${r.status === "closed" ? "badge-muted" : r.status === "review" ? "badge-warn" : "badge-live"}`}>{r.status}</span></td>
              </tr>))}
              {rows.length === 0 && <tr><td colSpan={2} className="muted">none yet</td></tr>}
            </tbody>
          </table>
        </div>
        {sel && (
          <div className="flex-1 card p-4 space-y-4">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="font-semibold">{sel.title}</div>
                <div className="mono muted text-xs">{sel.case_id} · analyst {sel.analyst} · {sel.status}</div>
                {sel.scope && <div className="text-xs muted mt-1">{sel.scope}</div>}
              </div>
              <div className="flex gap-2">
                <a className="btn" href={`${API}/api/v1/cases/${encodeURIComponent(sel.case_id)}/report`} target="_blank" rel="noopener noreferrer">JSON report</a>
                <a className="btn btn-primary" href={`${API}/api/v1/cases/${encodeURIComponent(sel.case_id)}/report.html`} target="_blank" rel="noopener noreferrer">Export HTML</a>
                <select className="field" style={{ width: 110 }} value={sel.status} onChange={(e) => run(async () => {
                  const st = e.target.value;
                  await api.put(`/api/v1/cases/${encodeURIComponent(sel.case_id)}`, { findings, status: st }); open(sel.case_id);
                })}>
                  {["open", "review", "closed"].map((s) => <option key={s}>{s}</option>)}
                </select>
              </div>
            </div>
            <div>
              <div className="kicker mb-2">Members ({sel.members.length})</div>
              <div className="flex flex-wrap gap-1.5">
                {sel.members.map((m, i) => <span key={i} className="badge">{m.kind}: <span className="mono">{m.ref_id}</span></span>)}
                {sel.members.length === 0 && <span className="muted text-xs">add clusters from the Clusters page</span>}
              </div>
            </div>
            <div>
              <div className="kicker mb-2">Analyst findings</div>
              <textarea className="field" rows={3} value={findings} onChange={(e) => setFindings(e.target.value)} />
              <button className="btn mt-2" onClick={() => run(async () => {
                await api.put(`/api/v1/cases/${encodeURIComponent(sel.case_id)}`, { findings }); open(sel.case_id);
              })}>save findings</button>
            </div>
            <div>
              <div className="kicker mb-2">Notes (append-only)</div>
              {sel.notes.map((n, i) => (
                <div key={i} className="text-xs py-1.5 border-b border-[var(--line)]">
                  <span className="muted mono">{n.at?.slice(0, 16)} {n.author}:</span> {n.text}
                </div>))}
              <div className="flex gap-2 mt-2">
                <input className="field" placeholder="add note…" value={note} onChange={(e) => setNote(e.target.value)}
                       onKeyDown={(e) => { if (e.key === "Enter" && note.trim()) run(async () => {
                         await api.post(`/api/v1/cases/${encodeURIComponent(sel.case_id)}/notes`, { text: note }); setNote(""); open(sel.case_id); }); }} />
                <button className="btn" onClick={() => { if (note.trim()) run(async () => {
                  await api.post(`/api/v1/cases/${encodeURIComponent(sel.case_id)}/notes`, { text: note }); setNote(""); open(sel.case_id); }); }}>add</button>
              </div>
            </div>
            <details>
              <summary className="kicker cursor-pointer">audit history ({sel.audit.length})</summary>
              {sel.audit.map((a, i) => <div key={i} className="mono muted text-[11px] py-0.5">{a.ts?.slice(0, 19)} · {a.actor} · {a.action}</div>)}
            </details>
          </div>
        )}
      </div>
    </div>
  );
}
