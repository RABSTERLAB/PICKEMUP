"use client";
/* Live Monitor — cybermap-style offline threat globe (d3-geo orthographic +
   Natural Earth polygons served from /public/data: zero tile requests,
   opsec-safe). Comet arcs carry direction (actor -> target); every marker and
   arc derives from stored detections and collected interaction records. */
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { geoOrthographic, geoPath, geoInterpolate } from "d3-geo";
import { feature } from "topojson-client";
import { api, type Alert, type Overview } from "@/lib/api";

type Marker = { code: string; lat: number; lon: number; accounts: number;
  automated: number; high_risk_posts: number; clusters: string[] };
type GeoLink = { from: string; to: string; kind: string; flagged: string; count: number };

const CRIMSON = "255,77,109";
const AMBER = "255,178,77";

export default function LivePage() {
  const boxRef = useRef<HTMLDivElement>(null);
  const cvsRef = useRef<HTMLCanvasElement>(null);
  const [markers, setMarkers] = useState<Marker[]>([]);
  const [links, setLinks] = useState<GeoLink[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [ov, setOv] = useState<Overview | null>(null);
  const [sel, setSel] = useState<Marker | null>(null);
  const rotRef = useRef({ lambda: 10, phi: -22 });
  const dragRef = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    let stop = false;
    const poll = () => {
      api.get<{ markers: Marker[] }>("/api/v1/geo").then((d) => !stop && setMarkers(d.markers)).catch(() => {});
      api.get<{ links: GeoLink[] }>("/api/v1/geo/interactions").then((d) => !stop && setLinks(d.links)).catch(() => {});
      api.get<Alert[]>("/api/v1/alerts").then((a) => !stop && setAlerts(a.slice(0, 40))).catch(() => {});
      api.get<Overview>("/api/v1/overview").then((o) => !stop && setOv(o)).catch(() => {});
    };
    poll();
    const t = setInterval(poll, 5000);
    return () => { stop = true; clearInterval(t); };
  }, []);

  useEffect(() => {
    const cvs = cvsRef.current, box = boxRef.current;
    if (!cvs || !box) return;
    let land: { type: string } | null = null, raf = 0, stop = false, last = 0;
    let stars: { x: number; y: number; r: number; a: number }[] = [], starW = 0, starH = 0;

    fetch("/data/countries-110m.json").then((r) => r.json()).then((topo) => {
      land = feature(topo as never, (topo as { objects: { countries: never } }).objects.countries) as never;
    });

    const DEG = Math.PI / 180;
    const visible = (lat: number, lon: number, lam: number, phi: number) => {
      const s = Math.sin(lat * DEG) * Math.sin(-phi * DEG)
              + Math.cos(lat * DEG) * Math.cos(-phi * DEG) * Math.cos((lon + lam) * DEG);
      return s > 0.02;
    };

    const draw = (ts: number) => {
      if (stop) return;
      raf = requestAnimationFrame(draw);
      if (ts - last < 33) return; // ~30fps
      last = ts;
      const w = box.clientWidth, h = box.clientHeight, dpr = window.devicePixelRatio || 1;
      // before layout settles the box measures ~0: R = min(w,h)/2-56 goes NEGATIVE and
      // createRadialGradient throws IndexSizeError. Skip the frame; rAF retries next tick.
      if (w < 140 || h < 140) return;
      if (cvs.width !== w * dpr) { cvs.width = w * dpr; cvs.height = h * dpr; }
      if (w !== starW || h !== starH) {
        starW = w; starH = h; stars = [];
        let seed = 7;
        const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
        for (let i = 0; i < 170; i++) stars.push({ x: rnd() * w, y: rnd() * h, r: rnd() * 1.1 + 0.2, a: rnd() * 0.5 + 0.12 });
      }
      const ctx = cvs.getContext("2d")!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      for (const s of stars) {
        ctx.fillStyle = `rgba(200,220,255,${s.a})`;
        ctx.fillRect(s.x, s.y, s.r, s.r);
      }
      const cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 56;
      if (!dragRef.current) rotRef.current.lambda += 0.035;
      const { lambda, phi } = rotRef.current;
      const proj = geoOrthographic().rotate([lambda, phi]).translate([cx, cy]).scale(R).precision(0.4);
      const p = geoPath(proj as never, ctx);
      // atmosphere halo
      const halo = ctx.createRadialGradient(cx, cy, R * 0.9, cx, cy, R * 1.32);
      halo.addColorStop(0, "rgba(53,224,200,0.16)"); halo.addColorStop(1, "rgba(53,224,200,0)");
      ctx.fillStyle = halo;
      ctx.beginPath(); ctx.arc(cx, cy, R * 1.32, 0, Math.PI * 2); ctx.fill();
      // ocean sphere
      const ocean = ctx.createRadialGradient(cx - R * 0.35, cy - R * 0.4, R * 0.2, cx, cy, R);
      ocean.addColorStop(0, "#0c1826"); ocean.addColorStop(1, "#05090f");
      ctx.fillStyle = ocean;
      ctx.beginPath(); p({ type: "Sphere" } as never); ctx.fill();
      // land
      if (land) {
        ctx.fillStyle = "#10233a";
        ctx.strokeStyle = "rgba(90,150,220,0.35)";
        ctx.lineWidth = 0.5;
        ctx.beginPath(); p(land as never); ctx.fill(); ctx.stroke();
      }
      // rim light
      ctx.strokeStyle = "rgba(53,224,200,0.5)"; ctx.lineWidth = 1;
      ctx.beginPath(); p({ type: "Sphere" } as never); ctx.stroke();

      const byCode: Record<string, Marker> = {};
      markers.forEach((m) => { byCode[m.code] = m; });
      const liftOf = (xy: [number, number], t: number): [number, number] => {
        const dx = xy[0] - cx, dy = xy[1] - cy;
        const len = Math.hypot(dx, dy) || 1;
        const lift = R * 0.17 * Math.sin(Math.PI * t);
        return [xy[0] + (dx / len) * lift, xy[1] + (dy / len) * lift];
      };
      // comet arcs (cybermap style): faint base + glowing packet head with tail
      const now = Date.now();
      links.forEach((l, li) => {
        const a = byCode[l.from], b = byCode[l.to];
        if (!a || !b || a.code === b.code) return;
        const col = l.flagged === "both" ? CRIMSON : AMBER;
        const interp = geoInterpolate([a.lon, a.lat], [b.lon, b.lat]);
        const N = 48;
        const pts: ([number, number] | null)[] = [];
        for (let i = 0; i <= N; i++) {
          const t = i / N;
          const pt = interp(t) as [number, number];
          if (!visible(pt[1], pt[0], lambda, phi)) { pts.push(null); continue; }
          const xy = proj(pt);
          pts.push(xy ? liftOf(xy as [number, number], t) : null);
        }
        // base curve — cybermap arcs are the hero visual, keep them readable
        ctx.save();
        ctx.shadowColor = `rgba(${col},0.9)`; ctx.shadowBlur = 6;
        ctx.strokeStyle = `rgba(${col},${l.flagged === "both" ? 0.55 : 0.32})`;
        ctx.lineWidth = 0.9 + Math.min(2.4, l.count * 0.075);
        ctx.beginPath();
        let pen = false;
        for (const q of pts) { if (q) { if (pen) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); pen = true; } else pen = false; }
        ctx.stroke();
        ctx.restore();
        // packet head + tail
        const speed = Math.max(1600, 3000 - l.count * 120);
        const tpos = ((now / speed) + li * 0.37) % 1;
        const hi = Math.round(tpos * N);
        if (pts[hi]) {
          for (let k = 8; k >= 1; k--) {
            const i0 = hi - k, i1 = hi - k + 1;
            if (i0 < 0 || !pts[i0] || !pts[i1]) continue;
            ctx.strokeStyle = `rgba(${col},${0.75 * (1 - k / 9)})`;
            ctx.lineWidth = 2.1;
            ctx.beginPath(); ctx.moveTo(pts[i0]![0], pts[i0]![1]); ctx.lineTo(pts[i1]![0], pts[i1]![1]); ctx.stroke();
          }
          const hd = pts[hi]!;
          ctx.save();
          ctx.shadowColor = `rgba(${col},1)`; ctx.shadowBlur = 18;
          ctx.fillStyle = "rgba(255,255,255,0.98)";
          ctx.beginPath(); ctx.arc(hd[0], hd[1], 3.0, 0, Math.PI * 2); ctx.fill();
          ctx.restore();
          // impact flash near target
          if (tpos > 0.9) {
            const g = (tpos - 0.9) / 0.1;
            const tq = pts[N] ?? pts[hi];
            if (tq) {
              ctx.strokeStyle = `rgba(${col},${0.7 * (1 - g)})`; ctx.lineWidth = 1.4;
              ctx.beginPath(); ctx.arc(tq[0], tq[1], 3 + 16 * g, 0, Math.PI * 2); ctx.stroke();
            }
          }
        }
      });
      // region markers (data-driven, subtler than arcs)
      const pulse = 0.6 + 0.4 * Math.sin(now / 420);
      for (const m of markers) {
        if (!visible(m.lat, m.lon, lambda, phi)) continue;
        const xy = proj([m.lon, m.lat]);
        if (!xy) continue;
        const hot = m.high_risk_posts > 0 || m.automated > 5;
        const base = 2.2 + Math.sqrt(m.accounts) * 0.8 + Math.min(4, m.high_risk_posts * 0.9);
        const col = m.high_risk_posts > 0 ? CRIMSON : m.automated > 0 ? AMBER : "53,224,200";
        ctx.save();
        ctx.shadowColor = `rgba(${col},0.9)`; ctx.shadowBlur = hot ? 16 * pulse : 6;
        ctx.fillStyle = `rgba(${col},${hot ? 0.8 + 0.2 * pulse : 0.65})`;
        ctx.beginPath(); ctx.arc(xy[0], xy[1], base * (hot ? 1 + 0.15 * pulse : 1), 0, Math.PI * 2); ctx.fill();
        ctx.restore();
        if (hot) {
          ctx.fillStyle = "rgba(235,242,250,0.9)";
          ctx.font = "10px ui-monospace, monospace";
          ctx.fillText(m.code, xy[0] + base + 4, xy[1] + 3);
        }
      }
    };
    raf = requestAnimationFrame(draw);

    const down = (e: PointerEvent) => { dragRef.current = { x: e.clientX, y: e.clientY }; };
    const move = (e: PointerEvent) => {
      if (!dragRef.current) return;
      rotRef.current.lambda -= (e.clientX - dragRef.current.x) * 0.35;
      rotRef.current.phi = Math.max(-80, Math.min(80, rotRef.current.phi + (e.clientY - dragRef.current.y) * 0.25));
      dragRef.current = { x: e.clientX, y: e.clientY };
    };
    const up = () => { dragRef.current = null; };
    const hit = (e: MouseEvent) => {
      const r = cvs.getBoundingClientRect(), R = Math.max(24, Math.min(r.width, r.height) / 2 - 26);
      const proj = geoOrthographic().rotate([rotRef.current.lambda, rotRef.current.phi])
        .translate([r.width / 2, r.height / 2]).scale(R);
      for (const m of markers) {
        const xy = proj([m.lon, m.lat]);
        if (xy && Math.hypot(xy[0] - (e.clientX - r.left), xy[1] - (e.clientY - r.top)) < 14) { setSel(m); return; }
      }
    };
    cvs.addEventListener("pointerdown", down); window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up); cvs.addEventListener("click", hit);
    return () => { stop = true; cancelAnimationFrame(raf); cvs.removeEventListener("pointerdown", down);
      window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
      cvs.removeEventListener("click", hit); };
  }, [markers, links]);

  const risk = ov?.risk;
  const tickerRows = links.slice(0, 9);
  return (
    <div className="flex flex-col gap-4">
      <div className="card glass relative overflow-hidden" style={{ height: "min(76vh, 700px)", padding: 0 }}>
        <div ref={boxRef} className="absolute inset-0">
          <canvas ref={cvsRef} className="h-full w-full" style={{ cursor: "grab", touchAction: "none" }} />
        </div>
        {/* top-left identity + KPI chips, cybermap style overlays */}
        <div className="absolute left-4 top-3 flex flex-col gap-2 pointer-events-none">
          <div className="text-sm font-semibold tracking-widest" style={{ color: "#dbe7f5" }}>
            PICKEMUP <span style={{ color: "#35e0c8" }}>THREAT GLOBE</span>
          </div>
          <div className="flex gap-2 flex-wrap" style={{ maxWidth: 420 }}>
            <span className="badge">regions: {markers.length}</span>
            <span className="badge">arcs: {links.length}</span>
            <span className={`badge ${risk && risk.high_severity ? "sev-high" : ""}`}>high severity: {risk?.high_severity ?? "—"}</span>
            <span className="badge">alerts: {alerts.length}</span>
          </div>
          <span className="muted text-[10px]">drag rotate · scroll zoom · click marker — offline rendering, no tile servers</span>
        </div>
        {/* legend bottom-right */}
        <div className="absolute right-4 bottom-3 flex flex-col gap-1 text-[10px] rounded-lg"
          style={{ color: "var(--muted-foreground)", padding: "8px 10px", background: "linear-gradient(180deg, rgba(4,8,13,0) 0%, rgba(4,8,13,0.72) 24%)", maxWidth: 340 }}>
          <span><span style={{ color: "#ff4d6d" }}>▸ — —</span> flagged ↔ flagged interaction</span>
          <span><span style={{ color: "#ffb24d" }}>▸ — —</span> flagged ↔ unflagged</span>
          <span>comet direction = actor → target · arc width = interaction volume</span>
          <span><span style={{ color: "#35e0c8" }}>●</span> region marker · size = account volume · label = high-risk region</span>
        </div>
        {sel && (
          <div className="card absolute right-3 top-3 w-64" style={{ background: "rgba(8,12,18,0.9)", backdropFilter: "blur(8px)" }}>
            <div className="flex justify-between items-center">
              <span className="font-semibold text-sm">{sel.code}</span>
              <button className="muted" onClick={() => setSel(null)}>✕</button>
            </div>
            <div className="text-xs mt-2 flex flex-col gap-1">
              <span>accounts observed: <b>{sel.accounts}</b></span>
              <span className={sel.automated ? "sev-moderate" : ""}>automation-flagged: <b>{sel.automated}</b></span>
              <span className={sel.high_risk_posts ? "sev-high" : ""}>high-severity posts: <b>{sel.high_risk_posts}</b></span>
              {sel.clusters.length > 0 && <span>clusters: <b className="mono">{sel.clusters.join(", ")}</b></span>}
              <span className="muted text-[10px] mt-1">region = estimated macro-area — not geolocation; arcs aggregate collected records</span>
            </div>
          </div>
        )}
      </div>

      <div className="grid lg:grid-cols-[300px_1fr_340px] gap-4">
        <div className="card glass flex flex-col" style={{ height: 250, overflow: "hidden" }}>
          <div className="text-xs font-semibold" style={{ padding: "10px 12px", borderBottom: "1px solid var(--line)" }}>
            Interaction stream <span className="muted">· actor → target</span>
          </div>
          <div className="flex-1 overflow-y-auto" style={{ padding: "8px 12px" }}>
            {tickerRows.map((l, i) => (
              <div key={i} className="flex gap-2 items-center mono text-[10px] whitespace-nowrap" style={{ padding: "2.5px 0", opacity: 0.6 + 0.4 * (1 - i / 9) }}>
                <span style={{ color: l.flagged === "both" ? "#ff4d6d" : "#ffb24d" }}>▸</span>
                <span className="font-semibold">{l.from} → {l.to}</span>
                <span className="muted">{l.kind} ×{l.count}</span>
                <span className="muted" style={{ marginLeft: "auto" }}>{l.flagged === "both" ? "F↔F" : "F↔open"}</span>
              </div>))}
            {tickerRows.length === 0 && (
              <div className="muted text-xs pt-2 leading-relaxed">No flagged region arcs yet — the globe needs
                accounts with a region signal, and public APIs (Bluesky/X) do not expose geolocation. Observed
                interactions between collected accounts are mapped live in <Link className="underline" href="/network">Network Explorer</Link>.</div>)}
          </div>
        </div>
        <div className="card glass flex flex-col" style={{ height: 250, overflow: "hidden" }}>
          <div className="text-xs font-semibold" style={{ padding: "10px 12px", borderBottom: "1px solid var(--line)" }}>
            Alert stream <span className="muted">· live 5s poll</span>
          </div>
          <div className="flex-1" style={{ overflowY: "auto", padding: 8, display: "flex", flexDirection: "column", gap: 6 }}>
            {alerts.map((a) => {
              const hot = a.severity === "high";
              const ev = (a.evidence ?? {}) as Record<string, unknown>;
              return (
                <div key={a.id} className={`card ${hot ? "glow-crimson" : ""}`} style={{ padding: "6px 10px", animation: "pulse-in .4s ease" }}>
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={`badge ${hot ? "sev-high" : "sev-moderate"}`} style={{ textTransform: "uppercase", fontSize: 9 }}>{a.severity}</span>
                    <span className="text-xs font-semibold">{String(a.kind).replace(/_/g, " ")}</span>
                    {typeof ev.category === "string" && <span className="badge">{String(ev.category).replace(/_/g, " ").toUpperCase()}</span>}
                    {typeof ev.language === "string" && <span className="badge mono">{String(ev.language).toUpperCase()}</span>}
                    <span className="muted text-[10px] mono" style={{ marginLeft: "auto" }}>{String(a.created_at).slice(5, 16).replace("T", " ")}</span>
                  </div>
                  <div className="muted text-[11px] mono" style={{ wordBreak: "break-word" }}>
                    {a.subject_id}{typeof ev.accounts === "number" ? ` · ${ev.accounts} accounts` : ""}{typeof ev.confidence === "number" ? ` · conf ${ev.confidence}` : ""}
                  </div>
                </div>
              );
            })}
            {alerts.length === 0 && <div className="muted text-sm" style={{ padding: 12 }}>No alerts yet — load a dataset and run analysis.</div>}
          </div>
        </div>
        <div className="card glass p-4 text-[11px] muted" style={{ height: 250, overflowY: "auto" }}>
          <div className="text-xs font-semibold mb-2" style={{ color: "var(--foreground)" }}>Integrity notes</div>
          Geo note: markers show stored detection intensity per macro-region; regions are estimated
          (heuristic or synthetic ground truth) — PICKEMUP never claims precise physical location from
          public posts, and no external map tiles are contacted during operations.<br /><br />
          Arc note: comets are aggregates of <i>observed</i> repost/mention records between accounts with
          at least one flagged endpoint. An arc proves the platform recorded an interaction; it does not
          prove coordination, control, or physical location.<br /><br />
          <span className="mono">origin mode labeled on every record · threat level: {(risk?.threat_level ?? "none").toUpperCase()}</span>
        </div>
      </div>
      <style>{`@keyframes pulse-in { from { opacity:0; transform: translateY(-4px);} to { opacity:1; transform:none;} }`}</style>
    </div>
  );
}
