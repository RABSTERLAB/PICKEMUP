"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type GraphSnap, type PostRow } from "@/lib/api";
import { OriginBadge } from "../shell";

const CLUSTER_COLORS = ["#35e0c8", "#6ea8fe", "#ffb454", "#ff5d6c", "#c678dd", "#98c379", "#e5c07b"];
const escHtml = (s: string) => s.replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string));
const OBSERVED_KINDS = new Set(["reposted", "quoted", "mentioned", "replied"]);
const WEAK_KINDS = new Set(["same_domain_weak", "temporal_sync"]);

type Sel =
  | { t: "node"; data: { id: string; label?: string } }
  | { t: "edge"; data: { source: string; target: string; kinds?: string; observed?: boolean; evidence?: string[] } }
  | null;

const isObserved = (kinds: string[] = []) => kinds.some((k) => OBSERVED_KINDS.has(k));
const isWeakOnly = (kinds: string[] = []) => kinds.length > 0 && kinds.every((k) => WEAK_KINDS.has(k));
const linkEndId = (v: any) => (v && typeof v === "object" ? v.id : v);

// deterministic bounding-sphere framing (library zoomToFit proved unreliable);
// p92 radius so stray outliers can't drag the camera into the void
function frameNodes(fg: any, ns: { x?: number; y?: number; z?: number }[], mult = 1.55, delay = 0, dur = 800) {
  setTimeout(() => {
    try {
      if (!ns.length) return;
      let bx = 0, by = 0, bz = 0;
      ns.forEach((n) => { bx += n.x || 0; by += n.y || 0; bz += n.z || 0; });
      bx /= ns.length; by /= ns.length; bz /= ns.length;
      const ds = ns.map((n) => Math.hypot((n.x || 0) - bx, (n.y || 0) - by, (n.z || 0) - bz)).sort((a, b) => a - b);
      const r = ds[Math.floor(ds.length * 0.92)] || 1;
      fg.cameraPosition({ x: bx, y: by, z: bz + r * mult }, { x: bx, y: by, z: bz }, dur);
    } catch { /* framing best-effort */ }
  }, delay);
}

export default function NetworkPage() {
  const boxRef = useRef<HTMLDivElement>(null);
  const labelBoxRef = useRef<HTMLDivElement>(null);
  const fgRef = useRef<any>(null);
  // latest swap snapshot for the per-engine-tick inertia guard (see swapData)
  const pinRef = useRef<Map<string, { x: number, y: number, z: number }> | null>(null);
  const selRef = useRef<string | null>(null);       // selection lives in a REF on purpose:
  const posBeforeCircleRef = useRef<{ id: string, x: number, y: number, z: number }[] | null>(null);
  const aliveRef = useRef(true);  // stale-render guard for async render()
  const circleRef = useRef<string | null>(null);    // putting it in render() deps destroyed and
  const fullDataRef = useRef<{ nodes: any[]; links: any[] }>({ nodes: [], links: [] }); // rebuilt fg on every click
  const rafRef = useRef(0);
  const [snap, setSnap] = useState<GraphSnap | null>(null);
  const [err, setErr] = useState("");
  const [clusterFilter, setClusterFilter] = useState("all");
  const [showInferred, setShowInferred] = useState(true);
  const [showWeak, setShowWeak] = useState(false);
  const [sel, setSel] = useState<Sel>(null);
  const [circleOf, setCircleOf] = useState<string | null>(null);
  const [explain, setExplain] = useState<string | null>(null);
  const [howto, setHowto] = useState(false);
  const interactedRef = useRef(false); // first deliberate click ends the idle auto-rotate
  const [evidence, setEvidence] = useState<any[] | null>(null);
  const deepApplied = useRef(false);

  // deep link: /network?node=<id|handle> opens that account's circle directly
  useEffect(() => {
    if (!snap || deepApplied.current) return;
    const nid = new URLSearchParams(window.location.search).get("node");
    if (!nid) return;
    deepApplied.current = true;
    const n = snap.nodes.find((x) => x.id === nid || x.label === nid);
    if (n) selectNode(n.id, n.label);
  }, [snap]);

  useEffect(() => {
    api.get<GraphSnap>("/api/v1/analysis/graph").then(setSnap).catch((e) => setErr(String(e)));
  }, []);

  const stopLabels = () => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = 0;
    if (labelBoxRef.current) labelBoxRef.current.innerHTML = "";
  };

  // circle mode: always-on labels for the (few) ego nodes, via per-frame projection.
  // Greedy screen-space declutter: ego first, then by degree; a label that would
  // collide with an already-placed one is hidden, and nothing is allowed off-edge.
  const startLabels = (fg: any, ns: any[]) => {
    stopLabels();
    const box = labelBoxRef.current;
    if (!box) return;
    const ego = circleRef.current;
    const els = ns.map((n) => {
      const d = document.createElement("div");
      const isEgo = n.id === ego;
      d.textContent = "@" + String(n.label ?? n.id);
      d.style.cssText = `position:absolute;left:0;top:0;font:10px/1.35 ui-monospace,monospace;white-space:nowrap;padding:1px 5px;border-radius:4px;opacity:0;transition:opacity .15s;pointer-events:none;z-index:${isEgo ? 10 : 5}`
        + (isEgo
          ? ";color:#04110d;background:rgba(53,224,200,.92);border:1px solid rgba(53,224,200,1);font-weight:700"
          : ";color:#dbe3ee;background:rgba(8,13,22,.78);border:1px solid rgba(138,160,180,.28)");
      box.appendChild(d);
      return { n, d, w: 0, prio: isEgo ? 1e9 : (n.degree ?? 1), box: null as null | { x: number, y: number, depth: number } };
    });
    const tick = () => {
      try {
        const w = fg.width(), h = fg.height();
        const cp = fg.cameraPosition(); // world position of camera
        const tgt = fg.controls()?.target ?? { x: 0, y: 0, z: 0 };
        let fx = tgt.x - cp.x, fy = tgt.y - cp.y, fz = tgt.z - cp.z;
        const fl = Math.hypot(fx, fy, fz) || 1; fx /= fl; fy /= fl; fz /= fl;
        for (const e of els) {
          const nx = +e.n.x || 0, ny = +e.n.y || 0, nz = +e.n.z || 0;
          const vx = nx - cp.x, vy = ny - cp.y, vz = nz - cp.z;
          const depth = vx * fx + vy * fy + vz * fz;
          const p = fg.graph2ScreenCoords(nx, ny, nz);
          e.box = depth > 1 && p && isFinite(p.x) && isFinite(p.y) ? { x: p.x, y: p.y, depth } : null;
        }
        const placed: { x1: number, y1: number, x2: number, y2: number }[] = [];
        for (const e of [...els].sort((a, b) => b.prio - a.prio)) {
          if (!e.box) { e.d.style.opacity = "0"; continue; }
          if (!e.w) { e.d.style.opacity = "1"; e.w = e.d.offsetWidth || 60; } // measurable once visible
          const bw = e.w, bh = 16;
          const yOff = e.prio > 1e8 ? 46 : 30; // keep pills clear of their own spheres (ego's is bigger)
          const x = Math.min(Math.max(e.box.x, bw / 2 + 2), w - bw / 2 - 2);
          const y = Math.min(Math.max(e.box.y - yOff, 64 + bh), h - 4); // 64px top keep-out = chip toolbar band
          const r = { x1: x - bw / 2, y1: y - bh, x2: x + bw / 2, y2: y + 2 };
          if (placed.some((q) => !(r.x2 < q.x1 || q.x2 < r.x1 || r.y2 < q.y1 || q.y2 < r.y1))) { e.d.style.opacity = "0"; continue; }
          placed.push(r);
          e.d.style.opacity = "1";
          e.d.style.transform = `translate(${Math.round(x - bw / 2)}px,${Math.round(y - bh)}px)`;
        }
      } catch { /* fg detached */ }
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
  };

  const selectNode = (id: string, label?: string) => {
    selRef.current = id;
    setSel({ t: "node", data: { id, label } });
    setCircleOf(id);
    try { const fg = fgRef.current; fg.nodeColor(fg.nodeColor()); } catch { /* re-eval trick */ }
  };

  const render = useCallback(async () => {
    if (!snap || !boxRef.current) return;
    let waited = 0;
    while (boxRef.current.clientHeight < 80 && waited < 2000) {
      await new Promise((r) => setTimeout(r, 100)); waited += 100;
    }
    if (!aliveRef.current) return;  // unmounted while waiting for height
    if (boxRef.current.clientHeight < 80) { setErr("graph container has no height — layout issue"); return; }
    (boxRef.current as any).__ro?.disconnect?.();
    stopLabels();
    circleRef.current = null;
    if (fgRef.current) { try { fgRef.current.pauseAnimation(); } catch { /* already gone */ } fgRef.current = null; }
    boxRef.current.innerHTML = "";
    const mod: any = await import("3d-force-graph");
    const ForceGraph3D = mod.default ?? mod;
    if (!aliveRef.current) return;  // unmounted during dynamic import

    let nodes = snap.nodes;
    if (clusterFilter === "rest") {
      nodes = nodes.filter((n) => !n.cluster || !(n.cluster in snap.clusters));
    } else if (clusterFilter !== "all") {
      const set = new Set(snap.clusters[clusterFilter] ?? []);
      nodes = nodes.filter((n) => set.has(n.id));
    }
    const ids = new Set(nodes.map((n) => n.id));
    const edges = snap.edges.filter((e) => ids.has(e.source) && ids.has(e.target)).filter((e) => {
      if (isObserved(e.kinds)) return true;
      if (isWeakOnly(e.kinds)) return showWeak;
      return showInferred;
    });
    if (!nodes.length) return;

    const clusterIds = Object.keys(snap.clusters);
    const colorOf = (c: string | null) => c && clusterIds.includes(c)
      ? CLUSTER_COLORS[clusterIds.indexOf(c) % CLUSTER_COLORS.length] : "#465063";
    const wpx = () => boxRef.current?.clientWidth ?? 800;
    const hpx = () => boxRef.current?.clientHeight ?? 600;

    const fg = ForceGraph3D({ controlType: "orbit" })(boxRef.current)
      .backgroundColor("#04070d")
      .width(wpx()).height(hpx())
      .graphData({ nodes: nodes.map((n) => ({ ...n })), links: edges.map((e) => ({ ...e })) })
      .enableNodeDrag(false) // the swap inertia guard pins coordinates; dragging must not fight it
      .nodeOpacity(0.95)     // selection must read as a solid sphere, not a translucent blob
      .showNavInfo(false)    // the near-invisible built-in hint only adds noise
      // circle mode = clean spokes: contact↔contact edges form a clique hairball that
      // visually buries the ego structure; hide them until we're back to the full graph
      .linkVisibility((l: any) => {
        const c = circleRef.current;
        if (!c) return true;
        return linkEndId(l.source) === c || linkEndId(l.target) === c;
      })
      // selection read from ref → recolored via nodeColor(nodeColor()) without recreating the instance
      .nodeColor((n: any) => (n.id === selRef.current ? "#ffffff" : colorOf(n.cluster)))
      .nodeVal((n: any) => (n.id === circleRef.current ? 3.2 : 1.6) + Math.min(6, (n.degree ?? 1) * 0.5))
      .nodeResolution(18)
      .nodeLabel((n: any) =>
        // collected handles/labels are UNTRUSTED text and this tooltip is
        // injected via innerHTML by 3d-force-graph — escape or it's XSS
        `<div style="font-family:ui-monospace,monospace;font-size:11px;color:#dbe3ee">
           <b>@${escHtml(String(n.label ?? ""))}</b><br/><span style="color:#8aa0b4">${escHtml(String(n.cluster ?? "unclustered"))} · deg ${Number(n.degree) || 0}</span>
         </div>`)
      .linkColor((l: any) => isObserved(l.kinds)
        ? "rgba(126,180,255,0.95)"
        : isWeakOnly(l.kinds) ? "rgba(138,160,180,0.22)" : "rgba(255,154,61,0.6)")
      .linkWidth((l: any) => 0.35 + Math.min(2.8, (l.weight ?? 1) * 0.8))
      .linkOpacity(0.9)
      .linkDirectionalParticles((l: any) => (isObserved(l.kinds) ? 2 : 0))
      .linkDirectionalParticleSpeed(0.006)
      .linkDirectionalParticleWidth(2.2)
      .linkDirectionalParticleColor(() => "#9ec5ff");
    try {
      const c = fg.controls();
      c.autoRotate = true; c.autoRotateSpeed = 0.55; c.enableDamping = true; c.dampingFactor = 0.08;
      // three's OrbitControls pointerup state-correction reads _pointerPositions[id].x
      // and throws `undefined.x` when a stale pointer id has no stored position (fires on
      // ordinary quick clicks: 3d-force-graph's DragControls relays `controls._onPointerUp`
      // with a pointer state OrbitControls never registered). Prune stale ids BEFORE the
      // original handler runs so its correction branch never sees them. Wrap the PRIVATE
      // bound props (that is what DragControls calls) plus public aliases for other builds.
      const prune = () => {
        const ids: any[] = c._pointers || [];
        const posMap = c._pointerPositions || {};
        for (let k = ids.length - 1; k >= 0; k--) if (posMap[ids[k]] === undefined) ids.splice(k, 1);
      };
      for (const [name, safe] of [["_onPointerUp", true], ["_onPointerMove", false]] as const) {
        const orig = c[name];
        if (typeof orig !== "function") continue;
        const bound = orig.bind(c);
        c[name] = (ev: any) => {
          try { prune(); bound(ev); }
          catch { if (safe) { try { c._pointers.length = 0; } catch { /* detached */ } } }
        };
      }
    } catch { /* controls unavailable */ }
    try { fg.d3Force("charge")?.strength?.(-260); } catch { /* force tweak is cosmetic */ }
    try { fg.d3Force("link")?.distance?.((l: any) => isObserved(l.kinds) ? 95 : 70); } catch { /* ditto */ }
    try { const cf = fg.d3Force("center"); if (cf?.strength) cf.strength(0.08); } catch { /* softer centering spreads clusters */ }
    try { fg.d3AlphaDecay(0.045).d3VelocityDecay(0.35); } catch { /* settle quickly, then frame */ }
    fg.onNodeClick((node: any) => {
      // ONE camera intent: clicking opens circle mode, and that framing owns the
      // camera. The old fly-to-node immediately followed by the ring refit was two
      // tweens fighting each other — the "weird animation" on every click.
      interactedRef.current = true;
      try { fg.controls().autoRotate = false; } catch { /* controls unavailable */ }
      selectNode(node.id, node.label);
    });
    fg.onBackgroundClick(() => { selRef.current = null; setSel(null); setCircleOf(null); });
    fgRef.current = fg;
    (window as any).__fg = fg;
    try {
      const gd0 = fg.graphData();
      fullDataRef.current = { nodes: gd0.nodes, links: gd0.links };
    } catch { /* capture for circle-mode swapping */ }
    // deterministic framing from the SETTLED bounding sphere; repeated so a late
    // layout drift can't leave the graph as a speck (library tweens proved unreliable)
    frameNodes(fg, fg.graphData().nodes, 1.55, 3400);
    frameNodes(fg, fg.graphData().nodes, 1.55, 7000);
    const ro = new ResizeObserver(() => { try { fg.width(wpx()).height(hpx()); } catch { /* detached */ } });
    ro.observe(boxRef.current);
    (boxRef.current as any).__ro = ro;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- sel/circleOf MUST NOT be deps (ref-driven by design)
  }, [snap, clusterFilter, showInferred, showWeak]);

  useEffect(() => {
    aliveRef.current = true;  // StrictMode remounts: the guard must re-arm
    return () => { aliveRef.current = false; };
  }, []);
  useEffect(() => { void render(); }, [render]);
  useEffect(() => () => { (boxRef.current as any)?.__ro?.disconnect?.(); stopLabels(); }, []);
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape") { selRef.current = null; setSel(null); setCircleOf(null); }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  // circle (ego-network) mode: swap graphData on the SAME sim objects; physics is frozen
  // for the ~250ms around each swap (alphaDecay 1 → sim stops next tick) so neither
  // entering nor leaving a circle re-relaxes the global layout ("no snap" requirement)
  useEffect(() => {
    const fg = fgRef.current;
    circleRef.current = circleOf;
    if (!fg || !snap) return;
    const swapData = (nodes: any[], links: any[]) => {
      // The engine hard-reheats (alpha=1) on every graphData rebuild and _buildGraph
      // re-applies stored physics props — physics-setting races can't be won from the
      // outside. Instead the inertia guard below runs INSIDE layoutTick's synchronous
      // stack (layout.tick() → onEngineTick, before positions are rendered), zeroing
      // alpha and writing coordinates back to the swap snapshot: zero visible snap,
      // entering or leaving circle mode. Node drag is disabled for this to hold.
      pinRef.current = new Map(nodes.map((n) => [n.id, { x: n.x, y: n.y, z: n.z }]));
      try { fg.graphData({ nodes, links }); } catch { /* detached */ }
      try { fg.d3ReheatSimulation(); } catch { /* detached */ } // keep engine ticking so
    };                                                            // the guard applies every frame
    // tween the inertia pin: positions interpolate from wherever they are now to
    // `targets` over dur ms — motion is applied exclusively through pinRef, so the
    // physics engine never participates (no forces, no drift, deterministic).
    let animTok = 0;
    const animatePin = (targets: Map<string, { x: number, y: number, z: number }>, dur = 800) => {
      const tok = ++animTok;
      const live = fg.graphData().nodes as any[];
      const start = new Map<string, { x: number, y: number, z: number }>();
      live.forEach((n) => start.set(n.id, { x: n.x, y: n.y, z: n.z }));
      targets.forEach((t, id) => { if (!start.has(id)) start.set(id, { ...t }); });
      const t0 = performance.now();
      const step = () => {
        if (tok !== animTok) return; // superseded by a newer intent
        const k = Math.min(1, (performance.now() - t0) / dur);
        const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
        const cur = new Map<string, { x: number, y: number, z: number }>();
        targets.forEach((tg, id) => {
          const s = start.get(id) ?? tg;
          cur.set(id, { x: s.x + (tg.x - s.x) * e, y: s.y + (tg.y - s.y) * e, z: s.z + (tg.z - s.z) * e });
        });
        pinRef.current = cur;
        try { fg.d3ReheatSimulation(); } catch { return; }
        if (k < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    };
    // capture the live d3-force-3d simulation: d3 calls force.initialize(sim) on
    // registration — the body itself must be the (no-op) per-tick force.
    try {
      let sim: any = null;
      const inertia: any = () => { /* no force contribution */ };
      inertia.initialize = (s: any) => { sim = s; };
      fg.d3Force("pickemup-inertia", inertia);
      fg.onEngineTick(() => {
        try { if (sim && sim.alpha() > 0) sim.alpha(0); } catch { /* detached */ }
        const pin = pinRef.current;
        if (!pin) return;
        (fg.graphData().nodes as any[]).forEach((n) => {
          const p = pin.get(n.id);
          if (!p) return;
          n.x = p.x; n.y = p.y; n.z = p.z; n.vx = 0; n.vy = 0; n.vz = 0;
        });
      });
    } catch { /* detached */ }
    try { fg.controls().autoRotate = !circleOf && !interactedRef.current; } catch { /* controls unavailable */ }
    if (circleOf) {
      const { nodes, links } = fullDataRef.current;
      const byId = new Map(nodes.map((n) => [n.id, n]));
      const ego = byId.get(circleOf);
      if (!ego) { setCircleOf(null); return; }
      const wsum = new Map<string, number>();
      links.forEach((l) => {
        const s = linkEndId(l.source), t = linkEndId(l.target);
        if (s === circleOf) wsum.set(t as string, (wsum.get(t as string) ?? 0) + (l.weight ?? 1));
        else if (t === circleOf) wsum.set(s as string, (wsum.get(s as string) ?? 0) + (l.weight ?? 1));
      });
      const top = [...wsum.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12).map(([k]) => k);
      const keep = new Set<string>([circleOf as string, ...top]);
      const ns = nodes.filter((n) => keep.has(n.id));
      const ls = links.filter((l) => keep.has(linkEndId(l.source) as string) && keep.has(linkEndId(l.target) as string));
      if (!posBeforeCircleRef.current) posBeforeCircleRef.current = nodes.map((n) => ({ id: n.id, x: n.x, y: n.y, z: n.z }));
      swapData(ns, ls);
      // deterministic ring: ego holds its position at the center, top contacts fan
      // evenly onto the XY ring around it (heaviest at top, counter-clockwise)
      const R = 150;
      const targets = new Map<string, { x: number, y: number, z: number }>();
      targets.set(circleOf, { x: ego.x || 0, y: ego.y || 0, z: ego.z || 0 });
      top.forEach((id, i) => {
        const a = Math.PI / 2 + (i / top.length) * Math.PI * 2;
        targets.set(id, { x: (ego.x || 0) + R * Math.cos(a), y: (ego.y || 0) + R * Math.sin(a), z: ego.z || 0 });
      });
      animatePin(targets, 850);
      // camera opens onto the ring WHILE it forms (not 700ms after) — one continuous motion
      frameNodes(fg, [...targets.values()], 2.2, 120, 900);
      startLabels(fg, ns);
    } else {
      const { nodes, links } = fullDataRef.current;
      if (nodes.length) {
        swapData(nodes, links); // inert swap: everything stays where it is right now
        const home = posBeforeCircleRef.current;
        if (home) animatePin(new Map(home.map((p) => [p.id, { x: p.x, y: p.y, z: p.z }])), 850);
        frameNodes(fg, home ?? nodes, 1.7, 250, 850); // refit as nodes are already homing
      }
      posBeforeCircleRef.current = null;
      stopLabels();
    }
  }, [circleOf, snap]);

  useEffect(() => {
    if (sel?.t === "edge" && sel.data.evidence?.length) {
      Promise.all(sel.data.evidence.slice(0, 4).map((id: string) =>
        api.get<any>(`/api/v1/posts/${encodeURIComponent(id)}/evidence`).catch(() => null)))
        .then((rs) => setEvidence(rs.filter(Boolean)));
    } else if (sel?.t === "node") {
      api.get<any>(`/api/v1/accounts/${encodeURIComponent(sel.data.id)}`)
        .then((d) => setEvidence((d.recent_posts ?? []).slice(0, 4))).catch(() => setEvidence(null));
    } else setEvidence(null);
  }, [sel]);

  // top contacts for the selected node, from stored edges only (no identity assumptions)
  const contacts = useMemo(() => {
    if (!snap || sel?.t !== "node") return [];
    const id = sel.data.id;
    const m = new Map<string, { w: number; kinds: Set<string>; obs: boolean }>();
    snap.edges.forEach((e) => {
      if (e.source !== id && e.target !== id) return;
      const o = e.source === id ? e.target : e.source;
      const cur = m.get(o) ?? { w: 0, kinds: new Set<string>(), obs: false };
      cur.w += e.weight ?? 1;
      (e.kinds ?? []).forEach((k) => cur.kinds.add(k));
      if (isObserved(e.kinds)) cur.obs = true;
      m.set(o, cur);
    });
    return [...m.entries()].map(([oid, v]) => {
      const n = snap.nodes.find((x) => x.id === oid);
      return { id: oid, w: v.w, kinds: v.kinds, obs: v.obs, label: n?.label ?? oid, cluster: n?.cluster ?? null };
    }).sort((a, b) => b.w - a.w).slice(0, 12);
  }, [snap, sel]);

  const shown = snap ? (clusterFilter === "all" ? snap.nodes.length
    : clusterFilter === "rest" ? snap.nodes.filter((n) => !n.cluster || !(n.cluster in snap.clusters)).length
      : (snap.clusters[clusterFilter] ?? []).length) : 0;
  const clusterIds = snap ? Object.keys(snap.clusters) : [];
  const restCount = snap ? snap.nodes.filter((n) => !n.cluster || !(n.cluster in snap.clusters)).length : 0;
  const circleLabel = sel?.t === "node" ? (sel.data.label ?? sel.data.id) : null;
  const meta = snap?.cluster_meta ?? {};

  // human names for engine signal keys, so the legend explains itself
  const SIGNAL_LABELS: Record<string, string> = {
    reposted: "reposts of the same source",
    mention: "co-mentions",
    shared_url: "shared the same link",
    similar_content: "near-duplicate text",
    synced_reposts: "reposts within seconds of each other",
    same_domain: "posts from the same domain (weak)",
    temporal: "bursty co-timing (weak)",
  };
  const sigName = (k: string) => SIGNAL_LABELS[k] ?? k.replace(/_/g, " ");

  return (
    <div className="space-y-4 h-[calc(100vh-3rem)] flex flex-col overflow-y-auto">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-xl font-semibold">Network Explorer</h1>
        {snap && <span className="muted text-xs">{shown === snap.nodes.length ? `${shown} accounts` : `showing ${shown} of ${snap.nodes.length} accounts`} · {snap.stats.edges} edges total · click a node to open its circle · drag rotate · scroll zoom</span>}
      </div>
      {err && <div className="card p-3 text-sm" style={{ color: "var(--danger)" }}>{err}</div>}
      <div className="flex flex-col lg:flex-row gap-4 flex-1 min-h-0">
        <div className="card flex-1 relative overflow-hidden flex flex-col">
          <div className="flex items-center gap-3 px-3 py-2 border-b border-[var(--line)] flex-wrap">
            <span className="kicker mr-0.5">behaviour groups</span>
            <div className="flex gap-1.5 flex-wrap">
              <button className={`badge ${clusterFilter === "all" ? "badge-live" : ""}`}
                onClick={() => { setCircleOf(null); setClusterFilter("all"); setExplain(null); }}>all</button>
              {clusterIds.map((c, i) => (
                <button key={c} title="isolate this group — click again to see WHY these accounts are grouped"
                  className={`badge ${clusterFilter === c ? "badge-live" : ""}`}
                  onClick={() => { setCircleOf(null); setHowto(false);
                    if (clusterFilter === c) setExplain(explain === c ? null : c);
                    else { setClusterFilter(c); setExplain(c); } }}>
                  <i style={{ width: 7, height: 7, borderRadius: 9, background: CLUSTER_COLORS[i % CLUSTER_COLORS.length], display: "inline-block", marginRight: 5 }} />
                  {c} · {snap?.clusters[c].length ?? 0}{meta[c] ? " ⓘ" : ""}
                </button>))}
              {restCount > 0 && (
                <button className={`badge ${clusterFilter === "rest" ? "badge-live" : ""}`}
                  onClick={() => { setCircleOf(null); setClusterFilter("rest"); setExplain(null); }}>
                  <i style={{ width: 7, height: 7, borderRadius: 9, background: "#465063", display: "inline-block", marginRight: 5 }} />
                  unclustered · {restCount}
                </button>)}
            </div>
            <label className="flex items-center gap-1.5 text-[11px] muted cursor-pointer">
              <input type="checkbox" checked={showInferred} onChange={(e) => { setCircleOf(null); setShowInferred(e.target.checked); }} />inferred signals</label>
            <label className="flex items-center gap-1.5 text-[11px] muted cursor-pointer">
              <input type="checkbox" checked={showWeak} onChange={(e) => { setCircleOf(null); setShowWeak(e.target.checked); }} />weak signals</label>
            {circleOf && (
              <button className="badge badge-live" title="exit circle view"
                onClick={() => { selRef.current = null; setSel(null); setCircleOf(null); }}>
                ◎ circle of @{circleLabel} — ✕ exit</button>)}
            <button className={`badge ${howto ? "badge-live" : ""}`} onClick={() => { setHowto((v) => !v); setExplain(null); }}>
              {howto ? "✕ close guide" : "? how to read"}</button>
            <div className="ml-auto flex gap-3 text-[11px] muted items-center">
              <span className="inline-flex items-center gap-1.5"><i style={{ width: 18, borderTop: "2px solid #6ea8fe", display: "inline-block" }} />observed (particles flow actor → target)</span>
              <span className="inline-flex items-center gap-1.5"><i style={{ width: 18, borderTop: "2px solid rgba(255,154,61,.8)", display: "inline-block" }} />inferred similarity</span>
            </div>
          </div>
          <div className="flex-1 relative min-h-0" style={{ minHeight: 420 }}>
            <div ref={boxRef} className="absolute inset-0 h-full w-full" />
            <div ref={labelBoxRef} className="absolute inset-0 pointer-events-none overflow-hidden" />
            {!snap && <div className="absolute inset-0 grid place-items-center muted text-sm">loading graph…</div>}
            {snap && shown === 0 && (
              <div className="absolute inset-0 grid place-items-center text-center px-6">
                <div className="muted text-sm">No stored coordination edges yet.<br />Load the demo dataset from Overview, then run analysis.</div>
              </div>)}
            {howto && (
              <div className="absolute top-3 right-3 w-[310px] rounded-lg border border-[var(--line)] p-3.5 text-[11px] leading-relaxed space-y-2"
                style={{ background: "rgba(6,10,17,.92)", backdropFilter: "blur(10px)", zIndex: 20 }}>
                <div className="kicker flex items-center">How to read this graph
                  <button className="ml-auto muted hover:text-[var(--fg)]" onClick={() => setHowto(false)}>✕</button></div>
                <ul className="space-y-1.5 list-none">
                  <li><b className="text-[var(--fg)]">Colored balls</b> = accounts. Same color = same behaviour group (the engines linked them by shared actions, <i>not</i> by identity).</li>
                  <li><b className="text-[var(--fg)]">Grey balls</b> = accounts with no coordination signal found — normal background population.</li>
                  <li><b className="text-[var(--fg)]">Ball size</b> = how connected the account is (degree).</li>
                  <li><b className="text-[var(--fg)]">Blue lines with particles</b> = OBSERVED records — someone actually reposted/mentioned someone; particles flow actor → target.</li>
                  <li><b className="text-[var(--fg)]">Orange lines</b> = INFERRED similarity (near-duplicate text, synced timing). A hint, not proof.</li>
                  <li><b className="text-[var(--fg)]">Click a node</b> → its circle: the 12 strongest contacts on a ring, heaviest at top. Click a group pill, then again for “why”.</li>
                  <li><b className="text-[var(--fg)]">Drag</b> rotates · <b className="text-[var(--fg)]">scroll</b> zooms · click empty space to exit.</li>
                </ul>
                <div className="muted italic">Groups answer “these moved together”, never “these are the same person”.</div>
              </div>)}
            {explain && meta[explain] && (() => {
              const m = meta[explain];
              const mix = Object.entries(m.signal_mix ?? {}).sort((a, b) => b[1] - a[1]);
              const maxMix = mix.length ? mix[0][1] : 1;
              const col = CLUSTER_COLORS[clusterIds.indexOf(explain) % CLUSTER_COLORS.length];
              return (
                <div className="absolute left-3 bottom-3 w-[360px] max-h-[calc(100%-24px)] overflow-y-auto rounded-lg border p-3.5 text-[11px] space-y-2.5"
                  style={{ background: "rgba(6,10,17,.93)", borderColor: col + "66", backdropFilter: "blur(10px)", zIndex: 20 }}>
                  <div className="flex items-center gap-2">
                    <i style={{ width: 10, height: 10, borderRadius: 9, background: col }} />
                    <b className="text-[12px]">Why {explain} is one group</b>
                    <span className={`badge ${m.verdict === "coordinated" ? "badge-warn" : ""}`}>{m.verdict}</span>
                    <span className="ml-auto muted" style={{ whiteSpace: "nowrap" }}>conf {Math.round((m.confidence ?? 0) * 100)}%</span>
                    <button className="muted hover:text-[var(--fg)]" onClick={() => setExplain(null)}>✕</button>
                  </div>
                  <div className="muted">{m.accounts ?? snap?.clusters[explain]?.length ?? 0} accounts · {m.edges ?? 0} behaviour links
                    {m.time_range ? ` · window ${m.time_range[0]?.slice(5, 16)} → ${m.time_range[1]?.slice(5, 16)}` : ""}</div>
                  <div className="space-y-1">
                    <div className="kicker">What tied them together</div>
                    {mix.map(([k, v]) => (
                      <div key={k} className="flex items-center gap-2">
                        <span className="w-[150px] shrink-0">{sigName(k)}</span>
                        <span className="h-1.5 rounded" style={{ width: `${Math.max(8, (v / maxMix) * 110)}px`, background: col, opacity: 0.85 }} />
                        <span className="mono muted">{v}</span>
                      </div>))}
                  </div>
                  {(m.shared_hashtags?.length || m.shared_domains?.length) ? (
                    <div>
                      <div className="kicker mb-1">Shared content</div>
                      <div className="flex flex-wrap gap-1">
                        {(m.shared_hashtags ?? []).slice(0, 6).map((h) => <span key={h} className="badge">{h}</span>)}
                        {(m.shared_domains ?? []).slice(0, 3).map((u) => <span key={u} className="badge mono" title={u}>{u.slice(0, 26)}</span>)}
                      </div>
                    </div>) : null}
                  {m.central_accounts?.length > 0 && (
                    <div>
                      <div className="kicker mb-1">Core of the group</div>
                      <div className="flex flex-wrap gap-1">
                        {m.central_accounts.slice(0, 5).map((h) => (
                          <button key={h} className="badge" style={{ borderColor: "rgba(53,224,200,.4)", color: "var(--accent)" }}
                            title="open this account's circle"
                            onClick={() => {
                              const n = snap?.nodes.find((x) => x.label === h || x.id === h);
                              if (n) { setExplain(null); selectNode(n.id, n.label); }
                            }}>@{h}</button>))}
                      </div>
                    </div>)}
                  {m.organic_explanations?.length > 0 && (
                    <div className="rounded-md border border-[rgba(255,178,77,.3)] bg-[rgba(255,178,77,.06)] p-2">
                      <span style={{ color: "var(--warn)" }}>Could still be organic:</span> {m.organic_explanations.join("; ")}.
                    </div>)}
                  <div className="muted italic border-t border-[var(--line)] pt-1.5">
                    Grouping is behavioural inference over collected records — not an identity, network-attribution or intent claim.
                  </div>
                </div>);
            })()}
          </div>
        </div>
        <div className="w-full lg:w-80 shrink-0 card p-4 overflow-y-auto">
          {!sel && <div className="muted text-sm">Select a node to open its circle — the accounts it most exchanges with — and inspect the evidence. Edges come only from observed behavioral records or scored similarity — never from identity assumptions. Weak signals (shared domain, loose timing) are hidden by default: alone they explain nothing.</div>}
          {sel?.t === "node" && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <div className="kicker">Circle of</div>
                <button className="btn ml-auto text-[10px]" style={{ padding: "2px 8px" }}
                  title="exit circle view — back to the full graph"
                  onClick={() => { selRef.current = null; setSel(null); setCircleOf(null); }}>✕ back to graph</button>
              </div>
              <div className="font-medium mono">@{sel.data.label ?? sel.data.id}</div>
              <div className="text-xs muted">cluster {snap?.nodes.find((n) => n.id === sel.data.id)?.cluster ?? "—"} · degree {snap?.nodes.find((n) => n.id === sel.data.id)?.degree ?? 0} · showing top {contacts.length} contacts</div>
              {(() => {
                const cl = snap?.nodes.find((n) => n.id === sel.data.id)?.cluster;
                const m = cl ? meta[cl] : undefined;
                if (!cl || !m) return null;
                const top = Object.entries(m.signal_mix ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 2)
                  .map(([k, v]) => `${sigName(k)} ×${v}`).join(", ");
                return (
                  <div className="text-[11px] rounded-md border border-[var(--line)] p-2">
                    <span className="kicker">Why grouped</span>
                    <div className="mt-1">one of {m.accounts ?? snap?.clusters[cl]?.length ?? "—"} accounts in {cl} — tied by {top || "shared behaviour signals"}</div>
                    <button className="mt-1 text-[10px]" style={{ color: "var(--accent)" }}
                      onClick={() => { setClusterFilter(cl!); setExplain(cl); setCircleOf(null); }}>full group analysis →</button>
                  </div>);
              })()}
              {contacts.length > 0 && (
                <div className="space-y-1.5">
                  <div className="kicker">Top contacts</div>
                  {contacts.map((ct) => (
                    <button key={ct.id} onClick={() => selectNode(ct.id, ct.label)}
                      className="w-full text-left card p-2 text-xs flex items-center gap-2 hover:border-[var(--accent)]" style={{ cursor: "pointer" }}>
                      <span className="mono truncate flex-1">@{ct.label}</span>
                      <span className={`badge ${ct.obs ? "badge-live" : "badge-warn"}`}>{ct.obs ? "observed" : "inferred"}</span>
                      <span className="muted mono">{ct.w}</span>
                    </button>))}
                  <div className="text-[10px] muted">weighted contact count from stored edges (repost/mention records = observed; scored similarity = inferred).</div>
                </div>)}
              {evidence?.map((p: PostRow) => (
                <div key={p.post_id} className="card p-2.5 text-xs space-y-1">
                  <div className="flex gap-2 items-center"><OriginBadge mode={p.origin_mode} /><span className="mono muted">{p.created_at?.slice(0, 16)}</span></div>
                  <div className="line-clamp-3">{(p.text || "[repost]").slice(0, 160)}</div>
                  <div className="mono muted truncate">sha256 {p.content_sha256?.slice(0, 20)}…</div>
                </div>))}
              <a className="text-xs" style={{ color: "var(--accent)" }} href={`/accounts?id=${encodeURIComponent(sel.data.id)}`}>open full account dossier →</a>
            </div>)}
          {sel?.t === "edge" && (
            <div className="space-y-3">
              <div className="kicker">Edge</div>
              <div className="mono text-xs break-all">{sel.data.source} ↔ {sel.data.target}</div>
              <div className="flex flex-wrap gap-1.5">{(sel.data.kinds ?? "").split(", ").filter(Boolean).map((k: string) =>
                <span key={k} className={`badge ${sel.data!.observed ? "badge-live" : "badge-warn"}`}>{k}</span>)}</div>
              <div className="text-[11px] muted">{sel.data.observed
                ? "OBSERVED relationship (repost/quote/mention chain in collected records)."
                : "INFERRED similarity from shared behavior — not proof of coordination or common control."}</div>
              {evidence?.map((p: any) => (
                <div key={p.post_id} className="card p-2.5 text-xs space-y-1">
                  <div className="flex gap-2 items-center"><OriginBadge mode={p.origin_mode} /><span className="mono muted">{String(p.created_at ?? "").slice(0, 16)}</span></div>
                  <div className="line-clamp-2">{(p.text || "[repost]").slice(0, 140)}</div>
                  {p.source_url && <a href={p.source_url} className="text-[var(--accent)] underline" target="_blank" rel="noopener noreferrer">source</a>}
                </div>))}
            </div>)}
        </div>
      </div>
    </div>
  );
}
