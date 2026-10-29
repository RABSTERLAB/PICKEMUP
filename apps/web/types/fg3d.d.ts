declare module "3d-force-graph" {
  export interface ForceGraph3DInstance {
    (opts?: Record<string, unknown>): ForceGraph3DInstance;
    graphData(data: { nodes: unknown[]; links: unknown[] }): ForceGraph3DInstance;
    width(px: number): ForceGraph3DInstance;
    height(px: number): ForceGraph3DInstance;
    backgroundColor(c: string): ForceGraph3DInstance;
    nodeColor(fnOrC: unknown): ForceGraph3DInstance;
    nodeVal(fn: unknown): ForceGraph3DInstance;
    nodeLabel(fn: unknown): ForceGraph3DInstance;
    nodeResolution(n: number): ForceGraph3DInstance;
    linkColor(fn: unknown): ForceGraph3DInstance;
    linkWidth(fn: unknown): ForceGraph3DInstance;
    linkOpacity(n: number): ForceGraph3DInstance;
    linkDirectionalParticles(fn: unknown): ForceGraph3DInstance;
    linkDirectionalParticleSpeed(fn: unknown): ForceGraph3DInstance;
    linkDirectionalParticleWidth(fn: unknown): ForceGraph3DInstance;
    linkDirectionalParticleColor(fn: unknown): ForceGraph3DInstance;
    onNodeClick(cb: (node: Record<string, unknown>, coords: { x: number; y: number }) => void): ForceGraph3DInstance;
    onBackgroundClick(cb: () => void): ForceGraph3DInstance;
    d3Force(name: string, force?: unknown): ForceGraph3DInstance;
    d3ReheatSimulation(): ForceGraph3DInstance;
    cameraPosition(pos: Record<string, unknown>, ms?: number): ForceGraph3DInstance;
    lookAt(point: Record<string, unknown>, ms?: number): ForceGraph3DInstance;
    pauseAnimation(): ForceGraph3DInstance;
    resumeAnimation(): ForceGraph3DInstance;
    controls(): OrbitControlsLike;
    renderer(): { domElement: HTMLElement };
    zoomToFit(ms?: number, padding?: number): ForceGraph3DInstance;
  }
  interface OrbitControlsLike {
    autoRotate: boolean;
    autoRotateSpeed: number;
    enableDamping: boolean;
    dampingFactor: number;
  }
  export default function ForceGraph3D(opts?: Record<string, unknown>): ForceGraph3DInstance;
}
