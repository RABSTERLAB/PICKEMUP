"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

type Src = Record<string, { available: boolean; reason: string; platform: string; capabilities: string[];
  cost_model: string; coverage_gaps: string[]; retention: string; freshness: string }>;
type Envelope = { connectors: Src; dev_tools: boolean };

type XCon = { token_set: boolean; token_source: "settings" | "env" | "none"; token_mask: string | null;
  per_job_usd: number; per_day_usd: number; caps_overridden: boolean; spend_today_usd: number;
  updated_at: string | null };

export default function SourcesPage() {
  const [env, setEnv] = useState<Envelope | null>(null);
  const [msg, setMsg] = useState("");
  const [xc, setXc] = useState<XCon | null>(null);
  const [token, setToken] = useState("");
  const [perJob, setPerJob] = useState("2.00");
  const [perDay, setPerDay] = useState("10.00");
  const [busy, setBusy] = useState("");
  const [probe, setProbe] = useState<{ ok: boolean; message: string } | null>(null);
  const [purgeWord, setPurgeWord] = useState("");

  const loadX = useCallback(() => api.get<XCon>("/api/v1/x-connection").then((c) => {
    setXc(c); setPerJob(String(c.per_job_usd)); setPerDay(String(c.per_day_usd));
  }).catch((e) => setMsg(String(e))), []);
  useEffect(() => {
    api.get<Envelope>("/api/v1/sources").then(setEnv).catch((e) => setMsg(String(e)));
    loadX();
  }, [loadX]);

  const act = (label: string, fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(label); setMsg(""); setProbe(null);
    fn().catch((e) => setMsg(String(e))).finally(() => setBusy(""));
  };

  return (
    <div className="space-y-4 max-w-5xl">
      <h1 className="text-xl font-semibold">Data Sources</h1>
      <p className="muted text-sm">Operational connectors and their honest manifests — capabilities, cost, coverage
        gaps and retention. To collect, use <span className="mono">Collect from X</span> on the Overview.</p>

      <div className="card p-4 space-y-3">
        <div className="flex items-center justify-between">
          <span className="kicker">X (Twitter) connection</span>
          <span className={`badge ${xc?.token_set ? "badge-ok" : "badge-muted"}`}>
            {xc ? (xc.token_set ? `token set — via ${xc.token_source}` : "no token") : "…"}
          </span>
        </div>
        <div className="text-xs muted">Stored locally in this app&apos;s database, shown masked, never returned by the API.
          Env / .env still works as a fallback; saving here takes priority.</div>
        <div className="flex flex-wrap gap-2 items-center">
          <input className="field flex-1" style={{ minWidth: 260 }} type="password" autoComplete="off"
            placeholder={xc?.token_mask ?? "paste App-only Bearer Token from developer.x.com"}
            value={token} onChange={(e) => setToken(e.target.value)} />
          <button className="btn btn-primary" disabled={busy !== "" || !token.trim()}
            onClick={() => act("save", async () => {
              setXc(await api.put<XCon>("/api/v1/x-connection", { token: token.trim() }));
              setToken(""); setMsg("token saved (shown masked from now on)");
            })}>{busy === "save" ? "saving…" : "save token"}</button>
          {xc?.token_set && (
            <button className="btn" disabled={busy !== ""} onClick={() => act("clear", async () => {
              setXc(await api.put<XCon>("/api/v1/x-connection", { token: "" }));
              setMsg("token cleared");
            })}>clear</button>
          )}
          <button className="btn" disabled={busy !== "" || !xc?.token_set}
            onClick={() => act("probe", async () => {
              setProbe(await api.post<{ ok: boolean; status: number; message: string }>(
                "/api/v1/x-connection/probe"));
            })}>{busy === "probe" ? "testing…" : "test connection"}</button>
        </div>
        {probe && (
          <div className="text-xs" style={{ color: probe.ok ? "var(--accent)" : "var(--warn)" }}>
            {probe.ok ? "✓ " : "⚠ "}{probe.message}
          </div>
        )}
        <div className="flex flex-wrap gap-4 items-center pt-2 border-t border-[var(--line)]">
          <div className="flex items-center gap-2 text-xs">
            <span className="muted">max $/job</span>
            <input className="field" style={{ width: 90 }} value={perJob} onChange={(e) => setPerJob(e.target.value)} />
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span className="muted">max $/day</span>
            <input className="field" style={{ width: 90 }} value={perDay} onChange={(e) => setPerDay(e.target.value)} />
          </div>
          <button className="btn" disabled={busy !== ""} onClick={() => act("caps", async () => {
            const j = Number(perJob), d = Number(perDay);
            if (!Number.isFinite(j) || !Number.isFinite(d) || j < 0 || d < 0) throw new Error("caps must be numbers ≥ 0");
            setXc(await api.put<XCon>("/api/v1/x-connection", { per_job_usd: j, per_day_usd: d }));
            setMsg("spend caps updated — enforced before every HTTP call");
          })}>{busy === "caps" ? "saving…" : "save caps"}</button>
          <span className="muted text-[11px]">
            spent today: ${xc ? xc.spend_today_usd.toFixed(3) : "—"}
            {xc?.caps_overridden ? " · caps from settings" : " · caps from defaults"}
          </span>
        </div>
      </div>

      <div className="grid md:grid-cols-2 gap-3">
        {env && Object.entries(env.connectors).map(([name, s]) => (
          <div key={name} className="card p-4 space-y-2">
            <div className="flex justify-between items-center">
              <span className="font-medium">{name}</span>
              <span className={`badge ${s.available ? "badge-ok" : "badge-warn"}`}>{s.available ? "available" : "unavailable"}</span>
            </div>
            <div className="text-xs muted">{s.reason}</div>
            <div className="flex flex-wrap gap-1.5">{s.capabilities.map((c) => <span key={c} className="badge">{c}</span>)}</div>
            <div className="text-[11px] muted space-y-0.5">
              <div>cost: {s.cost_model}</div>
              <div>freshness: {s.freshness}</div>
              <div>retention: {s.retention}</div>
              {s.coverage_gaps.map((g, i) => <div key={i}>gap: {g}</div>)}
            </div>
          </div>))}
      </div>
      {env && !env.dev_tools && (
        <div className="text-[11px] muted">Development/test sources (synthetic campaign, mock connector, file
          importers) are hidden and rejected by the API. Enable with <span className="mono">PICKEMUP_DEV_TOOLS=1</span> in .env.</div>
      )}

      <div className="card p-4 space-y-2" style={{ borderColor: "color-mix(in srgb, var(--danger) 40%, var(--line))" }}>
        <div className="kicker" style={{ color: "var(--danger)" }}>Danger zone — purge collected data</div>
        <div className="text-xs muted">Hard-deletes every collected/derived record (posts, accounts, detections,
          alerts, reports, cases, entity index, jobs). Connection settings, spend caps, watchlist terms and the
          audit trail survive.</div>
        <div className="flex gap-2 items-center">
          <input className="field" style={{ width: 160 }} placeholder="type PURGE to confirm"
            value={purgeWord} onChange={(e) => setPurgeWord(e.target.value)} />
          <button className="btn btn-danger" disabled={busy !== "" || purgeWord !== "PURGE"}
            onClick={() => act("purge", async () => {
              const r = await api.post<{ purged: Record<string, number> }>("/api/v1/data/purge", { confirm: "PURGE" });
              const total = Object.values(r.purged).reduce((a, b) => a + b, 0);
              setMsg(`purged ${total} records — settings & watchlist kept`);
              setPurgeWord("");
            })}>{busy === "purge" ? "purging…" : "purge all data"}</button>
        </div>
      </div>

      {msg && <div className="card p-2 text-xs" style={{ color: "var(--accent)" }}>{msg}</div>}
    </div>
  );
}
