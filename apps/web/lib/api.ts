export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8090";

/** API errors carry the HTTP status so callers can branch (401 vs 404 vs 500). */
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

const TIMEOUT_MS = 30000;

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  // path must be root-relative — never let a caller value redirect the origin
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new ApiError(0, `invalid API path: ${path}`);
  }
  const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), TIMEOUT_MS) : null;
  let r: Response;
  try {
    // internal timeout signal last: a caller-passed init.signal must never
    // silently disarm the timeout (and headers/method from init still apply)
    r = await fetch(`${API}${path}`, { ...init, cache: "no-store", signal: ctrl?.signal });
  } catch (e) {
    if (ctrl?.signal.aborted)
      throw new ApiError(0, "request timed out — is the API still responding?");
    if (e instanceof DOMException && e.name === "AbortError")  // caller-initiated cancel
      throw new ApiError(0, "request cancelled");
    throw new ApiError(0, "cannot reach the API — is the server running?");
  } finally {
    if (timer) clearTimeout(timer);
  }
  // 204 / empty / non-JSON bodies must not throw SyntaxError
  const text = await r.text();
  let data: unknown = null;
  if (text) {
    try { data = JSON.parse(text); }
    catch { data = { detail: text.slice(0, 200) }; }
  }
  if (!r.ok) {
    const rawDetail = data && typeof data === "object" && "detail" in data
      ? (data as { detail: unknown }).detail : undefined;
    const detail = typeof rawDetail === "string" ? rawDetail
      : Array.isArray(rawDetail)  // FastAPI 422 validation lists
        ? rawDetail.map((x: any) => String(x?.msg ?? JSON.stringify(x))).join("; ")
      : rawDetail != null ? JSON.stringify(rawDetail)
      : `HTTP ${r.status}`;
    throw new ApiError(r.status, detail.slice(0, 300));
  }
  return data as T;
}

export const api = {
  get: <T,>(path: string) => req<T>(path),
  post: <T,>(path: string, body?: unknown) =>
    req<T>(path, { method: "POST", headers: { "Content-Type": "application/json" },
                   body: JSON.stringify(body ?? {}) }),
  put: <T,>(path: string, body?: unknown) =>
    req<T>(path, { method: "PUT", headers: { "Content-Type": "application/json" },
                   body: JSON.stringify(body ?? {}) }),
  patch: <T,>(path: string, body?: unknown) =>
    req<T>(path, { method: "PATCH", headers: { "Content-Type": "application/json" },
                   body: JSON.stringify(body ?? {}) }),
  del: <T,>(path: string) => req<T>(path, { method: "DELETE" }),
};

export type Health = {
  status: string; version: string; env: string;
  db: { reachable: boolean; latency_ms: number; backend: string };
  worker: { alive: boolean; last_heartbeat: string | null; worker_id: string | null };
  counts: Record<string, number>;
};
export type Overview = {
  accounts_by_origin: Record<string, number>; posts_by_origin: Record<string, number>;
  risk?: { flagged: number; high_severity: number; by_category: Record<string, number>;
           threat_level: "low" | "moderate" | "high" | "critical" };
  top_clusters: { id: string; accounts: number; verdict: string; confidence: number; signals: Record<string, number> }[];
  recent_jobs: Job[]; api_spend_usd_by_day: Record<string, number>;
};
export type RiskItem = { post_id: string; category: string; score: number; severity: string;
  author_handle?: string | null;
  language: string; confidence: number; matched_terms: string[]; caveats: string[];
  excerpt: string; author_id: string; origin_mode: string; human_review_required: boolean;
  moderation?: { flagged: boolean; action: string; scores: { extremism_threat_score: number; profanity_harassment_score: number };
    detected_categories: string[]; matched_terms: string[]; reasoning: string } | null;
  algo_version: string };
export type RiskFeed = { count: number; items: RiskItem[]; note: string };
export type Job = { id: number; kind: string; status: string; attempts: number; error: string | null;
  result: Record<string, unknown>; created_at: string; finished_at: string | null; params: Record<string, unknown> };
export type AccountRow = { account_id: string; username: string; origin_mode: string;
  automation_verdict: string | null; automation_score: number | null; confidence: number | null };
export type AccountDetail = AccountRow & { display_name: string; followers: number | null; default_profile: boolean | null;
  ground_truth_label: string | null;
  created_at: string | null; following: number | null;
  automation: { verdict: string; score?: number; confidence?: number; signals?: { name: string; family: string; detail: string; score: number }[];
    weak_profile_signals?: string[];
    caveats?: string[]; evidence_post_ids?: string[] };
  cluster_memberships: { cluster: string; verdict: string; confidence: number }[];
  recent_posts: { post_id: string; text: string; created_at: string | null; hashtags: string[]; urls: string[];
    repost_of: string | null; source_url: string | null; content_sha256: string }[] };
export type Cluster = { cluster_id: string; verdict: string; confidence: number; members: string[]; accounts: number;
  edge_count: number; signal_mix: Record<string, number>; time_range: [string, string] | null;
  central_accounts: string[]; amplifiers: string[]; shared_domains: string[]; shared_hashtags: string[];
  organic_explanations: string[]; caveats: string[] };
export type ClusterMeta = { verdict: string; confidence: number; accounts: number | null;
  edges: number | null; signal_mix: Record<string, number>; shared_hashtags: string[];
  shared_domains: string[]; central_accounts: string[]; amplifiers: string[];
  organic_explanations: string[]; caveats: string[]; time_range: [string, string] | null };
export type GraphSnap = { nodes: { id: string; label: string; degree: number; cluster: string | null; size: number;
    origin_mode: string | null }[];
  edges: { source: string; target: string; kinds: string[]; weight: number; evidence: string[]; observed: boolean }[];
  clusters: Record<string, string[]>; stats: Record<string, number>;
  cluster_meta?: Record<string, ClusterMeta>;
  legend: { observed: string[]; inferred: string[] } };
export type Narrative = { narrative_id: string; query: string; post_count: number;
  first_observed: { post_id: string; author_id: string; at: string | null; text: string } | null;
  domains: string[]; hashtags: string[]; amplifiers: string[]; cascade: { post_id: string; author_id: string; at: string; kind: string }[];
  coverage_gaps: string[]; caveats: string[]; algo_version: string };
export type CaseRow = { case_id: string; title: string; status: string; analyst: string; created_at: string };
export type CaseDetail = CaseRow & { scope: string; findings: string;
  members: { kind: string; ref_id: string; added_at: string }[];
  notes: { author: string; text: string; at: string }[];
  audit: { ts: string; actor: string; action: string; detail: Record<string, unknown> }[] };
export type Alert = { id: number; kind: string; severity: string; subject_id: string;
  evidence: Record<string, unknown>; status: string; created_at: string;
  subject_label?: string; account_id?: string | null; excerpt?: string | null;
  post_url?: string | null };
export type PostRow = { post_id: string; author_id: string; text: string; created_at: string | null;
  hashtags: string[]; urls: string[]; repost_of: string | null; origin_mode: string;
  source_url: string | null; content_sha256: string };
