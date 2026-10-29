"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type Health } from "@/lib/api";

const NAV = [
  ["/", "Overview"],
  ["/live", "Live Monitor"],
  ["/network", "Network Explorer"],
  ["/search", "Search"],
  ["/accounts", "Account Intelligence"],
  ["/campaigns", "Coordination Clusters"],
  ["/narratives", "Narrative Origins"],
  ["/threats", "Threat Monitor"],
  ["/cases", "Investigations"],
  ["/alerts", "Alerts"],
  ["/reports", "Analyst Reports"],
  ["/sources", "Data Sources"],
];

export default function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const [health, setHealth] = useState<Health | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    const tick = () => api.get<Health>("/health").then((h) => { setHealth(h); setErr(false); })
      .catch(() => setErr(true));
    tick();
    const t = setInterval(tick, 10000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 border-r border-[var(--line)] p-4 flex flex-col gap-1 sticky top-0 h-screen">
        <div className="px-2 pt-2 pb-5">
          <div className="text-[15px] font-semibold tracking-wide">PICKEM<span className="text-[var(--accent)]">UP</span></div>
          <div className="kicker mt-1">Intelligence Console</div>
        </div>
        {NAV.map(([href, label]) => (
          <Link key={href} href={href} className={`nav-link ${path === href ? "active" : ""}`}>{label}</Link>
        ))}
        <div className="mt-auto card p-3 text-[11px] leading-relaxed">
          {err ? (
            <span className="badge badge-danger">API offline — run scripts/start-api.bat</span>
          ) : health ? (
            <div className="space-y-1">
              <div className="flex items-center gap-1.5">
                <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ background: health.worker.alive ? "var(--accent)" : "var(--warn)" }} />
                <span className="muted">api {health.version} · {health.db.backend} · worker {health.worker.alive ? "alive" : "idle"}</span>
              </div>
              <div className="muted text-[10px] break-words">{(health.counts.posts ?? 0).toLocaleString()} posts · {(health.counts.accounts ?? 0).toLocaleString()} accounts</div>
            </div>
          ) : <span className="muted">connecting…</span>}
        </div>
      </aside>
      <main className="flex-1 min-w-0 p-6 max-w-[1500px]">{children}</main>
    </div>
  );
}

export function OriginBadge({ mode }: { mode: string }) {
  const cls = mode === "synthetic" ? "badge-synthetic" : mode === "mock" ? "badge-mock"
    : mode === "fixture" ? "badge-fixture" : mode === "live" ? "badge-live" : "badge-muted";
  return <span className={`badge ${cls}`}>{mode}</span>;
}

export function VerdictBadge({ v }: { v: string | null }) {
  if (!v) return <span className="badge badge-muted">not analyzed</span>;
  const cls = v.includes("automated") || v.includes("coordinated_possible") ? "badge-danger"
    : v === "indeterminate" || v.includes("insufficient") ? "badge-warn" : "badge-ok";
  return <span className={`badge ${cls}`}>{v.replaceAll("_", " ")}</span>;
}
