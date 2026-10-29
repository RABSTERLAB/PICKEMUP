"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api, type AccountDetail, type AccountRow } from "@/lib/api";
import { OriginBadge, VerdictBadge } from "../shell";

export default function AccountsPage() {
  const [rows, setRows] = useState<AccountRow[]>([]);
  const [q, setQ] = useState("");
  const [verdict, setVerdict] = useState("");
  const [sel, setSel] = useState<AccountDetail | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const dseq = useRef(0);   // detail-open sequence guard  // newest-request-wins: stale poll responses never overwrite

  const load = () => {
    const n = ++seq.current;
    setLoading(true);  // stale rows must not masquerade as current while filters change
    api.get<AccountRow[]>("/api/v1/accounts" +
      `?${new URLSearchParams({ ...(q && { q }), ...(verdict && { verdict }), limit: "100" })}`)
      .then((r) => { if (n === seq.current) { setRows(Array.isArray(r) ? r : []); setErr(""); } })
      .catch((e) => { if (n === seq.current) setErr(String(e)); })
      .finally(() => { if (n === seq.current) setLoading(false); });
  };
  useEffect(() => {  // debounce keystrokes; 15s poll otherwise
    const t = setTimeout(load, 250);
    const iv = setInterval(load, 15000);
    return () => { clearTimeout(t); clearInterval(iv); };
  }, [q, verdict]);

  // URL mirrors the open dossier so refresh/share reload the same view
  const setUrlId = (id: string | null) => {
    const u = new URL(window.location.href);
    if (id) u.searchParams.set("id", id); else u.searchParams.delete("id");
    window.history.replaceState(null, "", u.toString());
  };
  const open = async (id: string) => {
    const n = ++dseq.current;  // last click wins even if an earlier one is slower
    try {
      const d = await api.get<AccountDetail>(`/api/v1/accounts/${encodeURIComponent(id)}`);
      if (n !== dseq.current) return;
      setSel(d); setUrlId(id);
    }
    catch (e) { if (n === dseq.current) setErr(String(e)); }
  };

  // deep link support: /accounts?id=<account_id> opens the dossier directly
  useEffect(() => {
    let alive = true;
    const id = new URLSearchParams(window.location.search).get("id");
    if (id) api.get<AccountDetail>(`/api/v1/accounts/${encodeURIComponent(id)}`)
      .then((d) => { if (alive) setSel(d); })
      .catch((e) => { if (alive) setErr(`Could not open account ${id}: ${e}`); });
    return () => { alive = false; };
  }, []);

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Account Intelligence</h1>
      <div className="flex gap-2 max-w-xl">
        <input className="field" placeholder="search username…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select className="field" style={{ width: 210 }} value={verdict} onChange={(e) => setVerdict(e.target.value)}>
          <option value="">any verdict</option>
          {["likely_automated", "likely_human", "indeterminate", "insufficient_evidence"].map((v) =>
            <option key={v} value={v}>{v.replaceAll("_", " ")}</option>)}
        </select>
      </div>
      {err && <div className="card p-3 text-sm" style={{ color: "var(--danger)" }}>{err}</div>}
      <div className="flex gap-4 items-start">
        <div className="card flex-1 overflow-hidden">
          <table className="tbl">
            <thead><tr><th>Username</th><th>Origin</th><th>Automation verdict</th><th>Score</th><th>Confidence</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.account_id} className="cursor-pointer" onClick={() => open(r.account_id)}>
                  <td className="font-medium">{r.username}</td>
                  <td><OriginBadge mode={r.origin_mode} /></td>
                  <td><VerdictBadge v={r.automation_verdict} /></td>
                  <td className="tabular-nums">{r.automation_score ?? "—"}</td>
                  <td className="tabular-nums muted">{r.confidence?.toFixed(2) ?? "—"}</td>
                </tr>
              ))}
              {loading && <tr><td colSpan={5} className="muted">Loading…</td></tr>}
              {!loading && rows.length === 0 && <tr><td colSpan={5} className="muted">No accounts — load data first.</td></tr>}
            </tbody>
          </table>
        </div>
        {sel && (
          <div className="w-[420px] shrink-0 card p-4 space-y-4 max-h-[75vh] overflow-y-auto sticky top-6">
            <div className="flex items-start justify-between">
              <div>
                <div className="font-semibold">{sel.display_name || sel.username}</div>
                <div className="mono muted text-xs">@{sel.username} · {sel.origin_mode}
                  {sel.ground_truth_label && <span className="badge badge-synthetic ml-2">GT: {sel.ground_truth_label}</span>}</div>
              </div>
              <button className="btn" onClick={() => { setSel(null); setUrlId(null); }}>close</button>
            </div>
            <div>
              <div className="kicker mb-2">Automation — <VerdictBadge v={sel.automation?.verdict ?? "insufficient_evidence"} /></div>
              <div className="confbar mb-1"><div style={{ width: `${sel.automation?.score ?? 0}%` }} /></div>
              <div className="muted text-[11px] mb-2">score {sel.automation?.score ?? "—"} · confidence {(sel.automation?.confidence ?? 0).toFixed(2)}</div>
              {(sel.automation?.signals ?? []).map((s, i) => (
                <div key={i} className="flex items-start gap-2 text-xs py-1.5 border-b border-[var(--line)]">
                  <span className={`badge ${s.family === "temporal" ? "badge-mock" : s.family === "content" ? "badge-warn" : "badge-muted"}`}>{s.family}</span>
                  <div><div>{s.name.replaceAll("_", " ")} — contribution {((s.score ?? 0) * 100).toFixed(0)}%</div>
                    <div className="muted text-[11px]">{s.detail}</div></div>
                </div>
              ))}
              {(sel.automation?.weak_profile_signals ?? []).length > 0 && (
                <div className="mt-1">
                  <div className="kicker">Weak profile cues — sparse history, not automation evidence</div>
                  {(sel.automation?.weak_profile_signals ?? []).map((w, i) => (
                    <div key={i} className="text-[11px] muted">• {w}</div>))}
                </div>)}
              <div className="text-[11px] mt-1">
                <div className="flex items-center gap-3">
                  <Link className="btn btn-primary" style={{ fontSize: 10, padding: "2px 10px" }}
                    title="enroll this account in the investigation queue: members, notes, findings and report export are ready for you"
                    href={`/cases?review=1&src=accounts&account=${encodeURIComponent(sel.account_id)}&handle=${encodeURIComponent(sel.username ?? "")}&cat=${encodeURIComponent(sel.automation?.verdict ?? "unknown")}&score=${sel.automation?.score ?? 0}&sev=${(sel.automation?.confidence ?? 0).toFixed(2)}`}>investigate →</Link>
                  <Link className="muted hover:underline" href={`/network?node=${encodeURIComponent(sel.account_id)}`}>show in network explorer →</Link>
                </div>
              </div>
              {sel.created_at && (
                <div className="text-[11px] muted mt-1">account created {sel.created_at.slice(0, 10)}
                  {" · "}{sel.followers ?? "?"} followers / {sel.following ?? "?"} following</div>)}
              {(sel.automation?.caveats ?? []).map((c, i) => <div key={i} className="text-[11px]" style={{ color: "var(--warn)" }}>⚠ {c}</div>)}
            </div>
            {(sel.cluster_memberships ?? []).length > 0 && (
              <div>
                <div className="kicker mb-2">Coordination clusters</div>
                {(sel.cluster_memberships ?? []).map((m) => (
                  <Link key={m.cluster} href="/campaigns" className="badge badge-danger mr-2">{m.cluster} · {((m.confidence ?? 0) * 100).toFixed(0)}%</Link>
                ))}
              </div>
            )}
            <div>
              <div className="kicker mb-2">Recent collected activity</div>
              {(sel.recent_posts ?? []).slice(0, 6).map((p) => (
                <div key={p.post_id} className="card p-2.5 text-xs mb-2 space-y-1">
                  <div className="muted mono flex justify-between">
                    <span>{p.created_at?.slice(0, 16)}</span>
                    <span title={p.content_sha256}>sha {p.content_sha256?.slice(0, 10)}…</span>
                  </div>
                  <div>{p.text ? p.text.slice(0, 200) : `[repost of ${p.repost_of}]`}</div>
                  {(p.hashtags ?? []).length > 0 && <div className="text-[var(--accent)]">{p.hashtags.map(h => `#${h}`).join(" ")}</div>}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
