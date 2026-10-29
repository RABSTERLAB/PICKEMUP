"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type Alert } from "@/lib/api";

const KIND_LABEL: Record<string, string> = {
  content_risk: "Content risk", automation: "Account automation",
  cluster: "Coordination cluster", edge: "Coordination edge",
  narrative: "Narrative origin", watchlist: "Watchlist hit",
};

export default function AlertsPage() {
  const [rows, setRows] = useState<Alert[]>([]);
  const [show, setShow] = useState<"new" | "">("new");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState<number | null>(null);      // one in-flight mutation at a time
  const [escalated, setEscalated] = useState<Record<number, string>>({});  // alert -> case id
  useEffect(() => {
    const load = () => api.get<Alert[]>(`/api/v1/alerts${show ? `?status=${show}` : ""}`)
      .then((r) => { setRows(Array.isArray(r) ? r : []); })
      .catch((e) => setErr(String(e)));
    load(); const t = setInterval(load, 8000); return () => clearInterval(t);
  }, [show]);
  const act = async (id: number, status: string) => {
    if (busy !== null) return;  // block double-submit
    setBusy(id); setErr("");
    try {
      await api.post(`/api/v1/alerts/${id}`, { status });
      setRows((r) => r.map((a) => (a.id === id ? { ...a, status } : a)));
    } catch (e) {
      setErr(`Could not update alert ${id}: ${e}`);  // row keeps its real status
    } finally {
      setBusy(null);
    }
  };
  const escalate = async (a: Alert) => {
    if (busy !== null) return;
    setBusy(a.id); setErr("");
    try {
      const r = await api.post<{ case_id: string }>("/api/v1/cases", {
        title: `Escalated: ${a.subject_label ?? a.subject_id} — ${(KIND_LABEL[a.kind] ?? a.kind).toLowerCase()}`,
        scope: "alert escalation", analyst: "operator",
      });
      setEscalated((m) => ({ ...m, [a.id]: r.case_id }));
    } catch (e) {
      setErr(`Could not open a case for alert ${a.id}: ${e}`);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Alerts</h1>
        <button className="btn" onClick={() => setShow(show === "" ? "new" : "")}>{show ? "showing: new only" : "showing: all"}</button>
      </div>
      <p className="muted text-sm max-w-2xl">Every alert names the account behind it and the exact words that triggered it.
        Deduplicated by subject+algo-version — no alert storms. Ack / false-positive decisions are recorded in the audit
        trail and keep engine output honest.</p>
      {err && <div className="card p-3 text-sm" style={{ color: "var(--danger)" }}>{err}</div>}
      <div className="card overflow-x-auto">
        <table className="tbl table-fixed">
          <colgroup>
            <col style={{ width: 104 }} /><col /><col style={{ width: 74 }} />
            <col style={{ width: 92 }} /><col style={{ width: 88 }} /><col style={{ width: 236 }} />
          </colgroup>
          <thead><tr><th>Kind</th><th>Who · what</th><th>Severity</th><th>When</th><th>Status</th><th /></tr></thead>
          <tbody>{rows.map((a) => {
            const matched = (a.evidence as Record<string, unknown>)?.matched_terms;
            const cats = (a.evidence as Record<string, unknown>)?.category;
            return (
            <tr key={a.id}>
              <td className="align-top text-xs">{KIND_LABEL[a.kind] ?? a.kind.replaceAll("_", " ")}</td>
              <td className="max-w-[420px]">
                {a.account_id ? (
                  <Link className="font-medium hover:underline" href={`/accounts?id=${encodeURIComponent(a.account_id)}`}>
                    @{a.subject_label ?? a.subject_id}</Link>
                ) : (
                  <span className="font-medium">{a.subject_label ?? a.subject_id}</span>
                )}
                {a.excerpt && (
                  <div className="text-xs muted mt-1 line-clamp-2">“{a.excerpt}”</div>)}
                {Array.isArray(matched) && matched.length > 0 && (
                  <div className="text-[10px] mt-1" style={{ color: "var(--warn, #ffb24d)" }}>
                    matched: {(matched as string[]).slice(0, 6).join(", ")}</div>)}
                {typeof cats === "string" && cats && (
                  <div className="text-[10px] muted mt-0.5">category: {cats.replaceAll("_", " ")}</div>)}
                <div className="text-[10px] muted mono mt-0.5 truncate" title={a.subject_id}>{a.subject_id}</div>
              </td>
              <td className="align-top"><span className={`badge ${a.severity === "high" ? "badge-danger" : a.severity === "medium" ? "badge-warn" : "badge-muted"}`}>{a.severity}</span></td>
              <td className="text-[11px] muted align-top">{a.created_at.slice(5, 16).replace("T", " ")}</td>
              <td className="align-top"><span className={`badge ${a.status === "new" ? "badge-live" : a.status === "false_positive" ? "badge-muted" : "badge-warn"}`}>{a.status.replaceAll("_", " ")}</span></td>
              <td className="align-top leading-6">
                {a.post_url && <a className="btn" href={a.post_url} target="_blank" rel="noreferrer" title="open the original post">post ↗</a>}{" "}
                {a.account_id && <Link className="btn" href={`/accounts?id=${encodeURIComponent(a.account_id)}`}>review →</Link>}{" "}
                {!escalated[a.id] && a.status !== "false_positive" &&
                  <button className="btn btn-danger" disabled={busy !== null} onClick={() => escalate(a)}>+ case</button>}
                {escalated[a.id] && <Link className="badge badge-live" href="/investigations">case {escalated[a.id]} →</Link>}{" "}
                {a.status === "new" && <>
                  <button className="btn" disabled={busy !== null} onClick={() => act(a.id, "acked")}>ack</button>{" "}
                  <button className="btn" disabled={busy !== null} onClick={() => act(a.id, "false_positive")}>false positive</button>
                </>}
              </td>
            </tr>);
          })}
            {rows.length === 0 && <tr><td colSpan={6} className="muted">No alerts in this view.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
