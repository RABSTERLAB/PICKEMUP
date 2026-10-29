"use client";

/* Analyst Reports — the optional-intelligence surface.
 * Reports are ALWAYS producible (deterministic template engine over stored
 * findings); a connected LLM upgrades narration and unlocks the autonomous
 * investigator. Delivery: in-app copy is the mandatory path — emailing
 * authorities is explicit, per-send opt-in, and audited. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";

type ReportRow = { id: string; scope: string; title: string; language: string; mode: string;
  model: string | null; content_md?: string; facts_sha256: string; facts?: Record<string, unknown>;
  llm_error: string | null; created_by: string; created_at: string;
  deliveries?: { id: number; recipients: string[]; kind: string; status: string; detail: string; at: string }[] };
type LLMStatus = { configured: boolean; usable: boolean; enabled: boolean; base_url: string;
  model: string; has_api_key: boolean; source: string };
type SMTPStatus = { host: string; port: number; username: string; from: string; tls: boolean;
  has_password: boolean; configured: boolean };
type MonitorStatus = { enabled: boolean; interval_minutes: number; auto_report: boolean;
  email_user: boolean; email_authorities: boolean; user_emails: string[];
  authorities_emails: string[]; language: string; last_run: string | null };

/* ---------- tiny safe markdown renderer (escape-first, no raw HTML) ---------- */
function Md({ src }: { src: string }) {
  const html = useMemo(() => {
    const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const out: string[] = [];
    let inUl = false;
    const closeUl = () => { if (inUl) { out.push("</ul>"); inUl = false; } };
    for (const raw of src.split("\n")) {
      const line = raw.trimEnd();
      if (!line.trim()) { closeUl(); continue; }
      let e = esc(line)
        .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
        .replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<i>$1</i>")
        .replace(/`([^`]+)`/g, "<code>$1</code>");
      const h = e.match(/^(#{1,4}) (.*)$/);
      if (h) { closeUl(); out.push(`<h${h[1].length}>${h[2]}</h${h[1].length}>`); continue; }
      if (/^[-*] /.test(line.trim()) && !line.startsWith("  ")) {
        if (!inUl) { out.push("<ul>"); inUl = true; }
        out.push(`<li>${e.replace(/^[-*] /, "")}</li>`); continue;
      }
      if (line.trim().startsWith("&gt;") || line.trim().startsWith(">")) {
        closeUl(); out.push(`<blockquote>${e.replace(/^>\s*/, "").replace(/^&gt;\s*/, "")}</blockquote>`); continue;
      }
      closeUl(); out.push(`<p>${e}</p>`);
    }
    closeUl();
    return out.join("");
  }, [src]);
  return <div className="md text-[13px] leading-relaxed" dir="auto" dangerouslySetInnerHTML={{ __html: html }} />;
}

const ModeBadge = ({ mode }: { mode: string }) =>
  mode === "llm"
    ? <span className="badge" style={{ borderColor: "rgba(53,224,200,.5)", color: "var(--accent)" }}>LLM narrative</span>
    : <span className="badge">deterministic</span>;

export default function ReportsPage() {
  const [llm, setLlm] = useState<LLMStatus | null>(null);
  const [smtp, setSmtp] = useState<SMTPStatus | null>(null);
  const [mon, setMon] = useState<MonitorStatus | null>(null);
  const [rows, setRows] = useState<ReportRow[]>([]);
  const [open, setOpen] = useState<ReportRow | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  // generate controls
  const [scope, setScope] = useState("snapshot");
  const [clusterIds, setClusterIds] = useState<string[]>([]);
  const [language, setLanguage] = useState<"en" | "ar">("en");
  const [useLlm, setUseLlm] = useState(true);

  // investigate controls
  const [focus, setFocus] = useState("");

  // settings forms
  const [showCfg, setShowCfg] = useState(false);
  const [llmForm, setLlmForm] = useState({ enabled: false, base_url: "", model: "", api_key: "" });
  const [smtpForm, setSmtpForm] = useState({ host: "", port: 587, username: "", password: "", from_address: "", tls: true });
  const [monForm, setMonForm] = useState({ enabled: false, interval_minutes: 30, auto_report: true,
    email_user: true, email_authorities: false, user_emails: "", authorities_emails: "", language: "en" });

  // send dialog
  const [sendTo, setSendTo] = useState("");
  const [sendKind, setSendKind] = useState<"user" | "authorities" | "custom">("authorities");
  const [sendAck, setSendAck] = useState(false);
  const [sendDlg, setSendDlg] = useState(false);

  const load = useCallback(() => {
    api.get<ReportRow[]>("/api/v1/ai/reports").then(setRows).catch(() => setRows([]));
    api.get<LLMStatus>("/api/v1/ai/status").then((s) => {
      setLlm(s as LLMStatus);
      return api.get<LLMStatus>("/api/v1/ai/config");
    }).then((c) => setLlmForm((f) => ({ ...f, enabled: c.enabled, base_url: c.base_url, model: c.model }))).catch(() => undefined);
    api.get<SMTPStatus>("/api/v1/ai/smtp").then((s) => {
      setSmtp(s);
      setSmtpForm((f) => ({ ...f, host: s.host, port: s.port, username: s.username, from_address: s.from, tls: s.tls }));
    }).catch(() => undefined);
    api.get<MonitorStatus>("/api/v1/ai/monitor").then((m) => {
      setMon(m);
      setMonForm({ enabled: m.enabled, interval_minutes: m.interval_minutes, auto_report: m.auto_report,
        email_user: m.email_user, email_authorities: m.email_authorities,
        user_emails: (m.user_emails || []).join(", "), authorities_emails: (m.authorities_emails || []).join(", "),
        language: m.language });
    }).catch(() => undefined);
    api.get<{ clusters: Record<string, string[]> }>("/api/v1/analysis/graph")
      .then((g) => setClusterIds(Object.keys(g.clusters || {}))).catch(() => undefined);
  }, []);
  useEffect(() => { load(); }, [load]);

  const act = async (label: string, fn: () => Promise<void>) => {
    setBusy(label); setErr(null); setNote(null);
    try { await fn(); } catch (e) { setErr(String(e).replace(/^Error: /, "")); }
    setBusy(null);
  };

  const generate = () => act("report", async () => {
    const r = await api.post<ReportRow>("/api/v1/ai/reports", { scope, language, use_llm: useLlm });
    setOpen(r); load();
    setNote(r.mode === "llm" ? "Narrative produced by the connected LLM over the deterministic fact-pack."
      : r.llm_error ? `LLM failed (${r.llm_error}) — deterministic template produced instead.`
      : "Produced by the deterministic writer (no LLM used).");
  });

  const investigate = () => act("investigate", async () => {
    const r = await api.post<ReportRow>("/api/v1/ai/investigate", { focus, max_steps: 6 });
    setOpen(r); load();
    setNote(`Investigation complete — ${(r.facts as { tool_steps?: unknown[] })?.tool_steps?.length ?? 0} tool calls.`);
  });

  const send = () => act("send", async () => {
    if (!open) return;
    const to = sendTo.split(/[,\s;]+/).map((s) => s.trim()).filter(Boolean);
    const d = await api.post<{ id: number; status: string; detail: string }>(`/api/v1/ai/reports/${open.id}/send`,
      { recipients: to, kind: sendKind });
    setSendDlg(false); setSendAck(false);
    setNote(d.status === "sent" ? `Email sent to ${to.length} recipient(s) — delivery audited (id ${d.id}).`
                                : `Delivery recorded as FAILED: ${d.detail}`);
    load();
  });

  const saveLlm = () => act("cfg-llm", async () => {
    const body: Record<string, unknown> = { enabled: llmForm.enabled, base_url: llmForm.base_url,
      model: llmForm.model, temperature: 0.2, max_tokens: 1600 };
    if (llmForm.api_key.trim()) body.api_key = llmForm.api_key.trim();
    await api.put("/api/v1/ai/config", body);
    setLlmForm((f) => ({ ...f, api_key: "" })); load();
    setNote("LLM connection saved.");
  });
  const testLlm = () => act("cfg-test", async () => {
    const r = await api.post<{ ok: boolean; detail: string }>("/api/v1/ai/probe", {});
    setNote(r.ok ? `LLM reachable — model replied: “${r.detail}”` : `LLM test failed: ${r.detail}`);
  });
  const saveSmtp = () => act("cfg-smtp", async () => {
    const body = { ...smtpForm };
    if (!smtpForm.password.trim()) delete (body as { password?: string }).password;
    await api.put("/api/v1/ai/smtp", body);
    setSmtpForm((f) => ({ ...f, password: "" })); load();
    setNote("SMTP settings saved.");
  });
  const saveMon = () => act("cfg-mon", async () => {
    await api.put("/api/v1/ai/monitor", { ...monForm,
      user_emails: monForm.user_emails.split(/[,\s;]+/).filter(Boolean),
      authorities_emails: monForm.authorities_emails.split(/[,\s;]+/).filter(Boolean) });
    load(); setNote("Monitor settings saved.");
  });

  const download = () => {
    if (!open) return;
    const blob = new Blob([open.content_md ?? ""], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `${open.id}.md`; a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="p-5 flex flex-col gap-4 max-w-[1500px]">
      <div>
        <h1 className="text-[19px] font-semibold">Analyst Reports</h1>
        <div className="muted text-[12px] mt-1">
          Structured findings are computed by the deterministic engines — reports never require an LLM.
          A connected model adds narrative polish and powers the autonomous investigator.
        </div>
        <div className="flex gap-1.5 mt-2 flex-wrap">
          <span className={llm?.usable ? "badge badge-live" : "badge"}>
            {llm?.usable ? `LLM: ${llm.model}` : "LLM: not connected — template mode"}
          </span>
          <span className={smtp?.configured ? "badge badge-live" : "badge"}>
            SMTP: {smtp?.configured ? smtp.host : "not configured"}
          </span>
          <span className={mon?.enabled ? "badge badge-live" : "badge"}>
            Monitor: {mon?.enabled ? `every ${mon.interval_minutes} min` : "off"}
            {mon?.last_run ? ` · last tick ${mon.last_run.slice(0, 16)}Z` : ""}
          </span>
          <button className="btn ml-auto" onClick={() => setShowCfg((v) => !v)}>
            {showCfg ? "Hide settings" : "Connection & delivery settings"}
          </button>
        </div>
      </div>

      {(err || note) && (
        <div className={`card p-3 text-[12px] ${err ? "text-[var(--danger)]" : "text-[var(--accent)]"}`}>
          {err ?? note}
        </div>
      )}

      {showCfg && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
          <div className="card p-3.5 space-y-2">
            <div className="kicker">LLM connection (optional)</div>
            <label className="flex items-center gap-2 text-[12px]">
              <input type="checkbox" checked={llmForm.enabled}
                onChange={(e) => setLlmForm({ ...llmForm, enabled: e.target.checked })} /> enabled
            </label>
            <input className="field" placeholder="https://api.openai.com/v1  ·  http://localhost:11434/v1"
              value={llmForm.base_url} onChange={(e) => setLlmForm({ ...llmForm, base_url: e.target.value })} />
            <input className="field" placeholder="model id (e.g. gpt-4.1-mini, qwen3, llama3.3)"
              value={llmForm.model} onChange={(e) => setLlmForm({ ...llmForm, model: e.target.value })} />
            <input className="field" type="password" placeholder="API key (stored server-side, never shown again)"
              value={llmForm.api_key} onChange={(e) => setLlmForm({ ...llmForm, api_key: e.target.value })} />
            <div className="flex gap-1.5">
              <button className="btn btn-primary" disabled={!!busy} onClick={saveLlm}>
                {busy === "cfg-llm" ? "Saving…" : "Save"}
              </button>
              <button className="btn" disabled={!!busy || !llm?.usable} onClick={testLlm}>
                {busy === "cfg-test" ? "Testing…" : "Test connection"}
              </button>
            </div>
            <div className="muted text-[11px]">Any OpenAI-compatible endpoint works (cloud or local).
              http:// is allowed only for loopback/private hosts.</div>
          </div>
          <div className="card p-3.5 space-y-2">
            <div className="kicker">SMTP for delivery (optional)</div>
            <div className="flex gap-1.5">
              <input className="field flex-1" placeholder="smtp host" value={smtpForm.host}
                onChange={(e) => setSmtpForm({ ...smtpForm, host: e.target.value })} />
              <input className="field w-20" type="number" value={smtpForm.port}
                onChange={(e) => setSmtpForm({ ...smtpForm, port: +e.target.value || 587 })} />
            </div>
            <input className="field" placeholder="from address" value={smtpForm.from_address}
              onChange={(e) => setSmtpForm({ ...smtpForm, from_address: e.target.value })} />
            <div className="flex gap-1.5">
              <input className="field flex-1" placeholder="username" value={smtpForm.username}
                onChange={(e) => setSmtpForm({ ...smtpForm, username: e.target.value })} />
              <input className="field flex-1" type="password" placeholder="password" value={smtpForm.password}
                onChange={(e) => setSmtpForm({ ...smtpForm, password: e.target.value })} />
            </div>
            <label className="flex items-center gap-2 text-[12px]">
              <input type="checkbox" checked={smtpForm.tls}
                onChange={(e) => setSmtpForm({ ...smtpForm, tls: e.target.checked })} /> STARTTLS
            </label>
            <button className="btn btn-primary" disabled={!!busy} onClick={saveSmtp}>
              {busy === "cfg-smtp" ? "Saving…" : "Save SMTP"}
            </button>
          </div>
          <div className="card p-3.5 space-y-2">
            <div className="kicker">Autonomous monitoring &amp; delivery</div>
            <label className="flex items-center gap-2 text-[12px]">
              <input type="checkbox" checked={monForm.enabled}
                onChange={(e) => setMonForm({ ...monForm, enabled: e.target.checked })} />
              monitor loop on (re-analyze, diff, alert, report)
            </label>
            <div className="flex gap-2 items-center text-[12px]">
              every <input className="field w-16" type="number" min={5} value={monForm.interval_minutes}
                onChange={(e) => setMonForm({ ...monForm, interval_minutes: +e.target.value || 30 })} /> minutes
              <select className="field w-20" value={monForm.language}
                onChange={(e) => setMonForm({ ...monForm, language: e.target.value })}>
                <option value="en">EN</option><option value="ar">AR</option>
              </select>
            </div>
            <label className="flex items-center gap-2 text-[12px]">
              <input type="checkbox" checked={monForm.auto_report}
                onChange={(e) => setMonForm({ ...monForm, auto_report: e.target.checked })} /> generate report on findings
            </label>
            <label className="flex items-center gap-2 text-[12px]">
              <input type="checkbox" checked={monForm.email_user}
                onChange={(e) => setMonForm({ ...monForm, email_user: e.target.checked })} /> email me (operator) — required path when SMTP is on
            </label>
            <label className="flex items-center gap-2 text-[12px]">
              <input type="checkbox" checked={monForm.email_authorities}
                onChange={(e) => setMonForm({ ...monForm, email_authorities: e.target.checked })} /> also email authorities (opt-in, actionable findings only)
            </label>
            <input className="field" placeholder="your emails (comma-separated)" value={monForm.user_emails}
              onChange={(e) => setMonForm({ ...monForm, user_emails: e.target.value })} />
            <input className="field" placeholder="authority emails (comma-separated)" value={monForm.authorities_emails}
              onChange={(e) => setMonForm({ ...monForm, authorities_emails: e.target.value })} />
            <button className="btn btn-primary" disabled={!!busy} onClick={saveMon}>
              {busy === "cfg-mon" ? "Saving…" : "Save monitor settings"}
            </button>
            <div className="muted text-[11px]">Every external email is recorded in the delivery audit trail.</div>
          </div>
        </div>
      )}

      <div className="flex gap-4 items-start">
        <div className="w-[330px] shrink-0 flex flex-col gap-3">
          <div className="card p-3.5 space-y-2">
            <div className="kicker">Generate report</div>
            <select className="field" value={scope} onChange={(e) => setScope(e.target.value)}>
              <option value="snapshot">Full corpus snapshot</option>
              <option value="alerts">Open alerts digest</option>
              {clusterIds.map((c) => <option key={c} value={`cluster:${c}`}>Cluster {c}</option>)}
            </select>
            <div className="flex gap-1.5">
              <button className={language === "en" ? "btn btn-primary" : "btn"} onClick={() => setLanguage("en")}>English</button>
              <button className={language === "ar" ? "btn btn-primary" : "btn"} onClick={() => setLanguage("ar")}>العربية</button>
            </div>
            <label className="flex items-center gap-2 text-[12px]">
              <input type="checkbox" checked={useLlm} disabled={!llm?.usable}
                onChange={(e) => setUseLlm(e.target.checked)} />
              narrate with LLM {!llm?.usable && <span className="muted">(connect one in settings)</span>}
            </label>
            <button className="btn btn-primary w-full" disabled={!!busy} onClick={generate}>
              {busy === "report" ? "Composing…" : "Generate report"}
            </button>
          </div>
          <div className="card p-3.5 space-y-2">
            <div className="kicker">Autonomous investigation</div>
            <textarea className="field min-h-[64px]" placeholder="What should the agent dig into? Empty = find the most significant anomaly. e.g. “who is amplifying the flagged cluster, and since when?”"
              value={focus} onChange={(e) => setFocus(e.target.value)} />
            <button className="btn w-full" disabled={!!busy || !llm?.usable} onClick={investigate}>
              {busy === "investigate" ? "Investigating (≤6 tool calls)…" : "Run investigator"}
            </button>
            {!llm?.usable && <div className="muted text-[11px]">Requires a connected LLM. Reports work without it.</div>}
          </div>
          <div className="card p-3.5 space-y-1.5">
            <div className="kicker">History</div>
            {rows.length === 0 && <div className="muted text-[12px]">No reports yet.</div>}
            <div className="max-h-[300px] overflow-y-auto flex flex-col gap-1">
              {rows.map((r) => (
                <button key={r.id} onClick={() => setOpen(r)}
                  className={`text-left rounded-md border px-2.5 py-1.5 text-[11px] transition-colors ${
                    open?.id === r.id ? "border-[rgba(53,224,200,.5)] bg-[var(--accent-dim)]" : "border-[var(--line)] hover:border-[rgba(53,224,200,.3)]"}`}>
                  <div className="font-medium truncate">{r.title}</div>
                  <div className="muted flex gap-1.5 items-center">
                    {r.created_at.slice(0, 16)}Z · <ModeBadge mode={r.mode} />
                    {r.created_by === "monitor" && <span className="badge">auto</span>}
                  </div>
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="card flex-1 min-h-[540px] p-0 overflow-hidden flex flex-col">
          {!open ? (
            <div className="flex-1 grid place-items-center text-center px-10">
              <div className="muted max-w-[420px] text-[13px] leading-relaxed">
                <div className="kicker mb-2">PICKEMUP REPORTING</div>
                Generate a snapshot report, drill a cluster, or let the investigator explore.
                Every report stores the exact deterministic fact-pack it was written from and a
                SHA-256 fingerprint, so any claim can be re-audited against the evidence.
              </div>
            </div>
          ) : (
            <>
              <div className="flex items-center gap-2 px-4 py-2.5 border-b border-[var(--line)] flex-wrap">
                <div className="font-medium text-[13px]" dir="auto">{open.title}</div>
                <ModeBadge mode={open.mode} />
                {open.model && <span className="badge">{open.model}</span>}
                <span className="badge">{open.language.toUpperCase()}</span>
                <div className="ml-auto flex gap-1.5">
                  <button className="btn" onClick={download}>Download .md</button>
                  <button className="btn" onClick={() => { setSendDlg((v) => !v); setSendTo(sendKind === "authorities"
                    ? (mon?.authorities_emails || []).join(", ") : (mon?.user_emails || []).join(", ")); }}>
                    Email…
                  </button>
                </div>
              </div>
              {sendDlg && (
                <div className="px-4 py-3 border-b border-[var(--line)] bg-[rgba(255,178,77,.05)] space-y-2">
                  <div className="kicker" style={{ color: "var(--warn)" }}>External delivery — audited &amp; opt-in</div>
                  <div className="flex gap-1.5">
                    {(["user", "authorities", "custom"] as const).map((k) => (
                      <button key={k} className={sendKind === k ? "btn btn-primary" : "btn"} onClick={() => {
                        setSendKind(k);
                        setSendTo(k === "authorities" ? (mon?.authorities_emails || []).join(", ")
                          : k === "user" ? (mon?.user_emails || []).join(", ") : "");
                      }}>{k}</button>
                    ))}
                  </div>
                  <input className="field" placeholder="recipient emails" value={sendTo} onChange={(e) => setSendTo(e.target.value)} />
                  <label className="flex items-center gap-2 text-[12px]">
                    <input type="checkbox" checked={sendAck} onChange={(e) => setSendAck(e.target.checked)} />
                    I understand this sends the report outside this machine. Findings are inference, not proof.
                  </label>
                  <div className="flex gap-1.5">
                    <button className="btn btn-danger" disabled={!sendAck || !sendTo.trim() || !!busy} onClick={send}>
                      {busy === "send" ? "Sending…" : `Send ${sendKind} email`}
                    </button>
                    <button className="btn" onClick={() => setSendDlg(false)}>Cancel</button>
                  </div>
                </div>
              )}
              <div className="flex-1 overflow-y-auto p-5">
                {open.facts && Array.isArray((open.facts as { tool_steps?: unknown[] }).tool_steps) && (
                  <div className="mb-4 card p-3 text-[11px] space-y-0.5">
                    <div className="kicker mb-1">Investigation trail</div>
                    {((open.facts as { tool_steps: { tool: string; args: Record<string, unknown>; error?: boolean }[] }).tool_steps)
                      .map((s, i) => (
                        <div key={i} className="mono">
                          {i + 1}. {s.tool}({JSON.stringify(s.args)}){s.error ? " — error fed back" : ""}
                        </div>
                      ))}
                  </div>
                )}
                <div dir={open.language === "ar" ? "rtl" : "ltr"}>
                  <Md src={open.content_md ?? ""} />
                </div>
                <div className="muted text-[10px] mt-6 border-t border-[var(--line)] pt-2 mono">
                  facts_sha256 {open.facts_sha256} · algo-version pinned in report ·{" "}
                  {open.deliveries?.length ?? 0} audited deliveries
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
