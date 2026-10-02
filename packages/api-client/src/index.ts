/**
 * Typed client for the AURA control-plane API.
 *
 * Browser apps call same-origin `/v1/...` (proxied to the API by Next rewrites), so the
 * HttpOnly session cookie travels without CORS. Unsafe methods send `x-aura-csrf: 1`,
 * which the API requires for cookie-authenticated writes (cross-site forms cannot set it).
 *
 * Types mirror the API schemas; `pnpm gen:api` can regenerate exact types from OpenAPI.
 */

export interface Problem {
  type: string;
  title: string;
  status: number;
  detail?: string;
  code: string;
  [extra: string]: unknown;
}

export class ApiError extends Error {
  constructor(public readonly problem: Problem) {
    super(problem.detail ?? problem.title);
  }
  get status(): number {
    return this.problem.status;
  }
}

type Method = "GET" | "POST" | "PATCH" | "DELETE";

export async function api<T>(
  path: string,
  opts: { method?: Method; body?: unknown; form?: FormData; base?: string } = {},
): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = {};
  if (method !== "GET") headers["x-aura-csrf"] = "1";
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const res = await fetch(`${opts.base ?? ""}${path}`, {
    method,
    headers,
    body,
    credentials: "include",
  });
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") ?? "";
  const data: unknown = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const p = (typeof data === "object" && data !== null ? data : {}) as Partial<Problem>;
    throw new ApiError({
      type: p.type ?? "about:blank",
      title: p.title ?? res.statusText,
      status: res.status,
      detail: p.detail ?? (typeof data === "string" ? data : undefined),
      code: p.code ?? "http_error",
      ...p,
    });
  }
  return data as T;
}

// ---- resources -------------------------------------------------------------------------

export interface User {
  id: string;
  email: string;
  name: string | null;
  is_superadmin: boolean;
}

export interface AccountAccess {
  account_id: string;
  kind: "individual" | "organization";
  tier: string;
  name: string;
  role: string;
  organization_id: string | null;
  slug: string | null;
  permissions: string[];
}

export interface Me {
  user: User;
  accounts: AccountAccess[];
}

export interface Organization {
  id: string;
  account_id: string;
  name: string;
  slug: string;
  workspace_id: string;
  project_id: string;
  environment_ids: Record<string, string>;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  environment_id: string | null;
  permissions: string[];
  created_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

export interface Entitlement {
  feature: string;
  enabled: boolean;
  limit: number | null;
  period: string | null;
  used?: number | null;
}

export interface Plan {
  code: string;
  name: string;
  audience: "b2c" | "b2b";
  price_minor: number | null;
  currency: string;
  interval: string;
  trial_days: number;
  entitlements: Entitlement[];
}

export interface Subscription {
  id: string;
  plan_code: string;
  plan_name: string;
  plan_version: number;
  status: string;
  price_minor: number | null;
  currency: string;
  current_period_start: string | null;
  current_period_end: string | null;
  trial_ends_at: string | null;
  cancel_at_period_end: boolean;
}

export interface ProviderCredential {
  id: string;
  provider: string;
  label: string;
  base_url: string | null;
  last4: string;
  created_at: string | null;
  revoked_at: string | null;
}

export interface XRayOpportunity {
  kind: string;
  title: string;
  workload: string;
  affected_requests: number;
  potential_monthly_savings_usd: string;
  expected_monthly_savings_usd: string;
  confidence: number;
  recommendation: string;
}

export interface XRayWorkload {
  key: string;
  label: string;
  requests: number;
  monthly_spend_usd: string;
  est_monthly_savings_usd: string;
  models: string[];
  input_tokens_p50: number;
}

export interface XRayReport {
  summary: {
    requests: number;
    monthly_spend_usd: string;
    est_monthly_savings_usd: string;
    potential_monthly_savings_usd: string;
    est_savings_pct: number;
    projected_monthly_spend_usd: string;
    unpriced_requests: number;
    methodology: string;
  };
  models: { model: string; tier: string; requests: number; monthly_spend_usd: string; share_of_spend: number }[];
  workloads: XRayWorkload[];
  opportunities: XRayOpportunity[];
}

export interface XRaySummary {
  id: string;
  name: string;
  source: string;
  status: string;
  record_count: number;
  created_at: string;
  summary: Partial<XRayReport["summary"]>;
  report?: XRayReport;
}

export interface ShadowCandidate {
  model: string;
  samples: number;
  non_inferiority_rate?: number;
  non_inferiority_ci?: [number, number];
  cost_change_pct?: number;
  deploy_recommended?: boolean;
  gate_reasons?: string[];
  projection?: { monthly_baseline_usd: string; monthly_candidate_usd: string; monthly_savings_usd: string };
}

export interface ShadowRun {
  id: string;
  name: string;
  status: "queued" | "running" | "completed" | "failed";
  config: { candidates: string[]; sample_size: number; budget_usd: string; workload: string };
  results: { candidates?: ShadowCandidate[]; budget_exhausted?: boolean; method_note?: string };
  spent_usd: string;
  error: string | null;
  created_at: string;
}

// ---- endpoint helpers ------------------------------------------------------------------

export const auth = {
  requestLink: (email: string, app: "web" | "admin" = "web") =>
    api<{ status: string }>("/v1/auth/magic-link", { method: "POST", body: { email, app } }),
  verify: (token: string) => api<User>("/v1/auth/magic-link/verify", { method: "POST", body: { token } }),
  logout: () => api<void>("/v1/auth/logout", { method: "POST" }),
  me: () => api<Me>("/v1/me"),
};

export const orgs = {
  create: (name: string) => api<Organization>("/v1/organizations", { method: "POST", body: { name } }),
};

const acct = (id: string) => `/v1/accounts/${encodeURIComponent(id)}`;

export const accounts = {
  apiKeys: (id: string) => api<ApiKey[]>(`${acct(id)}/api-keys`),
  createApiKey: (id: string, body: { name: string; environment_id?: string | null; permissions?: string[] }) =>
    api<ApiKey & { key: string }>(`${acct(id)}/api-keys`, { method: "POST", body }),
  revokeApiKey: (id: string, keyId: string) => api<void>(`${acct(id)}/api-keys/${keyId}`, { method: "DELETE" }),
  entitlements: (id: string) => api<Entitlement[]>(`${acct(id)}/entitlements`),
  subscription: (id: string) => api<Subscription>(`${acct(id)}/subscription`),
  checkout: (id: string, plan: string) =>
    api<{ checkout_url: string }>(`${acct(id)}/subscription/checkout`, { method: "POST", body: { plan } }),
  credits: (id: string) => api<{ balance: number; history: { kind: string; amount: number; memo: string; created_at: string }[] }>(`${acct(id)}/credits`),
  providerCredentials: (id: string) => api<ProviderCredential[]>(`${acct(id)}/provider-credentials`),
  addProviderCredential: (id: string, body: { provider: string; api_key: string; label?: string; base_url?: string }) =>
    api<ProviderCredential>(`${acct(id)}/provider-credentials`, { method: "POST", body }),
  revokeProviderCredential: (id: string, credId: string) =>
    api<void>(`${acct(id)}/provider-credentials/${credId}`, { method: "DELETE" }),
  xrays: (id: string) => api<XRaySummary[]>(`${acct(id)}/xray`),
  xray: (id: string, reportId: string) => api<XRaySummary>(`${acct(id)}/xray/${reportId}`),
  uploadXray: (id: string, form: FormData) => api<XRaySummary>(`${acct(id)}/xray`, { method: "POST", form }),
  shadowRuns: (id: string) => api<ShadowRun[]>(`${acct(id)}/shadow-runs`),
  shadowRun: (id: string, runId: string) => api<ShadowRun>(`${acct(id)}/shadow-runs/${runId}`),
  createShadowRun: (id: string, form: FormData) => api<ShadowRun>(`${acct(id)}/shadow-runs`, { method: "POST", form }),
};

export interface GraphRunSummary {
  id: string;
  status: string;
  template: string | null;
  cost_usd: string;
  error: string | null;
  created_at: string;
  trace: {
    status?: string;
    escalations?: number;
    warnings?: string[];
    nodes?: { id: string; kind: string; status: string; model: string | null; cost_usd: string; latency_ms: number; error: string | null }[];
  };
}

export const runs = {
  list: (accountId: string) => api<GraphRunSummary[]>(`${acct(accountId)}/runs`),
};

export const plans = {
  list: (audience?: "b2c" | "b2b") => api<Plan[]>(`/v1/plans${audience ? `?audience=${audience}` : ""}`),
};

export const xrayPublic = {
  submit: (form: FormData) => api<{ report: XRayReport }>("/v1/xray/public", { method: "POST", form }),
};

export const admin = {
  enroll: () => api<{ secret: string; otpauth_uri: string }>("/v1/admin/auth/totp/enroll", { method: "POST" }),
  verify: (code: string) => api<void>("/v1/admin/auth/totp/verify", { method: "POST", body: { code } }),
  logout: () => api<void>("/v1/admin/auth/logout", { method: "POST" }),
  metrics: () => api<Record<string, unknown> & { mrr_inr: Record<string, string>; arr_inr: string }>("/v1/admin/metrics"),
  accounts: (params: { q?: string; kind?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)]));
    return api<{ account_id: string; kind: string; status: string; name: string; plan: string | null; created_at: string }[]>(`/v1/admin/accounts?${qs}`);
  },
  account: (id: string) => api<Record<string, unknown>>(`/v1/admin/accounts/${id}`),
  suspend: (id: string, reason: string) => api<void>(`/v1/admin/accounts/${id}/suspend`, { method: "POST", body: { reason } }),
  restore: (id: string, reason: string) => api<void>(`/v1/admin/accounts/${id}/restore`, { method: "POST", body: { reason } }),
  changePlan: (id: string, plan: string, reason: string) => api<unknown>(`/v1/admin/accounts/${id}/plan`, { method: "POST", body: { plan, reason } }),
  credits: (id: string, amount: number, reason: string, kind = "grant") =>
    api<{ balance: number }>(`/v1/admin/accounts/${id}/credits`, { method: "POST", body: { amount, reason, kind } }),
  leads: (stage?: string) => api<{ id: string; email: string; company: string | null; stage: string; summary: Record<string, unknown>; created_at: string }[]>(`/v1/admin/leads${stage ? `?stage=${stage}` : ""}`),
  setLeadStage: (id: string, stage: string) => api<void>(`/v1/admin/leads/${id}`, { method: "PATCH", body: { stage } }),
  economics: (month?: string) =>
    api<{
      start: string;
      accounts: number;
      revenue_inr: string;
      cost_inr: string;
      margin_inr: string;
      gross_margin_pct: number | null;
      negative_margin_accounts: number;
      alert: boolean;
      worst: { account_id: string; revenue_inr: string; cost_inr: string; margin_inr: string }[];
    }>(`/v1/admin/economics${month ? `?month=${month}` : ""}`),
  audit: (accountId?: string) => api<{ id: string; at: string; actor_type: string; actor_id: string | null; account_id: string | null; action: string }[]>(`/v1/admin/audit${accountId ? `?account_id=${accountId}` : ""}`),
};

// ---- AURA Personal (B2C) -----------------------------------------------------------------

/** The runtime is proxied at /rt on the web origin (same-origin cookie, no CORS). */
export const RUNTIME_BASE = "/rt";

export interface AuraBlock {
  execution_id: string;
  model: string;
  provider: string;
  tier: string;
  cost_usd: string;
  baseline_cost_usd: string | null;
  saved_usd: string;
  latency_ms: number;
  credential_source: "platform" | "byok" | "cache";
  routing: { reason: string; candidates: string[] };
  attempts: { model: string; error: string | null }[];
  notes: string[];
  usage: { input_tokens: number; output_tokens: number };
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export type Mode = "auto" | "fast" | "balanced" | "best";

export interface ShareCard {
  id: string;
  model: string;
  tier: string;
  mode: string;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: string;
  baseline_cost_usd: string | null;
  saved_usd: string;
  saved_pct: number;
  cached: boolean;
  optimizations: string[];
}

export const personal = {
  chat: (body: { messages: ChatTurn[]; mode: Mode; sessionId: string; model?: string }) =>
    api<{ choices: { message: ChatTurn }[]; aura: AuraBlock }>("/v1/chat/completions", {
      method: "POST",
      base: RUNTIME_BASE,
      body: {
        model: body.model ?? `aura/${body.mode}`,
        messages: body.messages,
        aura: { session_id: body.sessionId },
      },
    }),
  memories: () =>
    api<{ data: { id: string; kind: string; content: string }[] }>("/v1/me/memories", { base: RUNTIME_BASE }),
  addMemory: (content: string) =>
    api<{ id: string }>("/v1/me/memories", { method: "POST", base: RUNTIME_BASE, body: { content } }),
  deleteMemory: (id: string) => api<void>(`/v1/me/memories/${id}`, { method: "DELETE", base: RUNTIME_BASE }),
  forgetAll: () => api<{ deleted: number }>("/v1/me/memories", { method: "DELETE", base: RUNTIME_BASE }),
  creditPacks: () =>
    api<{ code: string; name: string; credits: number; price_minor: number; currency: string }[]>("/v1/credit-packs"),
  buyCredits: (accountId: string, pack: string) =>
    api<{ provider: string; order_id: string; amount_minor: number; currency: string; key_id: string }>(
      `${acct(accountId)}/credits/purchase`,
      { method: "POST", body: { pack } },
    ),
  referral: () => api<{ code: string; referred: number; credits_earned: number }>("/v1/me/referral"),
  redeem: (code: string) => api<{ credits: number }>("/v1/me/referral/redeem", { method: "POST", body: { code } }),
  share: (accountId: string, executionId: string) =>
    api<ShareCard>(`${acct(accountId)}/shares`, { method: "POST", body: { execution_id: executionId } }),
  publicCard: (id: string) => api<ShareCard>(`/v1/shares/${id}`),
};

// ---- chat: saved conversations (runtime) ----------------------------------------------------

export interface Conversation {
  id: string;
  title: string;
  mode: Mode;
  created_at: string;
  updated_at: string;
}

export interface SubqueryPart {
  model: string | null;
  cost_usd: string;
  status: string | null;
}

export type StoredAura =
  | ({ kind: "single" } & AuraBlock)
  | {
      kind: "subqueries";
      execution_id: string;
      status: string;
      cost_usd: string;
      subqueries: SubqueryPart[];
      combine_model: string | null;
      combine_cost_usd: string;
      models: string[];
      notes: string[];
    }
  | AgentSummary;

/** Content-free numbers about a multi-agent answer (stored unencrypted). */
export interface AgentSummary {
  kind: "agents";
  cost_usd: string;
  calls: number;
  ms: number;
  nodes: number;
  max_depth: number;
  follow_ups: number;
  statuses: Record<string, number>;
  drafts: number;
  reviews: number;
  revisions: number;
  models: { drafter: string[]; reviewer: string[]; finaliser: string | null };
  warnings: string[];
}

/** The reviewed tree (decrypted for the owner only). */
export interface AgentTurn {
  role: "drafter" | "reviewer";
  model: string;
  text: string;
  cost_usd: string;
  ms: number;
  prompt: string;
  verdict: "approve" | "revise" | null;
  score: number | null;
  issues: string[];
  follow_ups: string[];
  parsed: boolean;
}

export interface AgentNode {
  id: string;
  question: string;
  depth: number;
  origin: "split" | "follow_up";
  status: "approved" | "unresolved" | "unreviewed" | "skipped_cap" | "failed" | "pending";
  answer: string;
  issues: string[];
  turns: AgentTurn[];
  children: AgentNode[];
}

export type MessageTrace = { version: 1; tree: AgentNode[] } | { subqueries: string[] };

/** Live progress from the agents while an answer is being made. */
export type AgentEvent =
  | { type: "splitting" }
  | { type: "split"; nodes: { id: string; question: string }[] }
  | { type: "draft"; node: string; attempt: number; model: string; cost_usd: string }
  | {
      type: "review";
      node: string;
      attempt: number;
      model: string;
      verdict: "approve" | "revise";
      score: number | null;
      issues: string[];
      follow_ups: number;
    }
  | { type: "revise"; node: string; attempt: number }
  | { type: "review_failed"; node: string; reason: string }
  | { type: "follow_up"; node: string; parent: string; depth: number; question: string }
  | { type: "node_done"; node: string; status: AgentNode["status"]; reason?: string; error?: string }
  | { type: "finalise"; model: null; skipped: boolean }
  | { type: "finalised"; model: string; cost_usd: string };

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  aura: StoredAura | null;
  trace?: MessageTrace | null;
  created_at: string;
}

export type ChatEvent =
  | { type: "user"; message: ChatMessage }
  | { type: "plan"; subqueries: string[] }
  | { type: "agent"; event: AgentEvent }
  | { type: "delta"; text: string }
  | { type: "done"; message: ChatMessage }
  | { type: "error"; problem: Problem };

export interface ModelInfo {
  id: string;
  owned_by: string;
  aura?: {
    display_name: string;
    usable: boolean;
    local: boolean;
    tier: "small" | "medium" | "frontier";
    context_window: number;
    prices_usd_per_mtok: { input: string; output: string };
  };
}

export const models = {
  list: () => api<{ data: ModelInfo[] }>("/v1/models", { base: RUNTIME_BASE }),
};

export const chat = {
  list: () => api<{ data: Conversation[] }>("/v1/conversations", { base: RUNTIME_BASE }),
  create: (mode: Mode = "auto") =>
    api<Conversation>("/v1/conversations", { method: "POST", base: RUNTIME_BASE, body: { mode } }),
  get: (id: string) =>
    api<Conversation & { messages: ChatMessage[] }>(`/v1/conversations/${id}`, { base: RUNTIME_BASE }),
  update: (id: string, body: { title?: string; mode?: Mode }) =>
    api<Conversation>(`/v1/conversations/${id}`, { method: "PATCH", base: RUNTIME_BASE, body }),
  remove: (id: string) => api<void>(`/v1/conversations/${id}`, { method: "DELETE", base: RUNTIME_BASE }),
  /** Cancels the answer being written on the server (keeps the part already written). */
  stop: (id: string) =>
    api<{ stopped: boolean }>(`/v1/conversations/${id}/stop`, { method: "POST", base: RUNTIME_BASE }),
  status: (id: string) => api<{ answering: boolean }>(`/v1/conversations/${id}/status`, { base: RUNTIME_BASE }),

  /** Streams a turn. Resolves when the stream ends; abort with ``signal`` to stop. */
  async send(
    id: string,
    body: { content: string; mode: Mode; model?: string },
    onEvent: (e: ChatEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const res = await fetch(`${RUNTIME_BASE}/v1/conversations/${id}/messages`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-aura-csrf": "1" },
      body: JSON.stringify(body),
      credentials: "include",
      signal,
    });
    if (!res.ok || !res.body) {
      const p = (await res.json().catch(() => ({}))) as Partial<Problem>;
      throw new ApiError({
        type: p.type ?? "about:blank",
        title: p.title ?? res.statusText,
        status: res.status,
        detail: p.detail,
        code: p.code ?? "http_error",
        ...p,
      });
    }
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let cut: number;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        let event = "message";
        let data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event: ")) event = line.slice(7);
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        if (!data) continue;
        const payload = JSON.parse(data) as Record<string, unknown>;
        if (event === "user") onEvent({ type: "user", message: payload as unknown as ChatMessage });
        else if (event === "plan") onEvent({ type: "plan", subqueries: payload.subqueries as string[] });
        else if (event === "agent") onEvent({ type: "agent", event: payload as unknown as AgentEvent });
        else if (event === "delta") onEvent({ type: "delta", text: String(payload.text ?? "") });
        else if (event === "done") onEvent({ type: "done", message: payload as unknown as ChatMessage });
        else if (event === "error") onEvent({ type: "error", problem: payload as unknown as Problem });
      }
    }
  },
};

export function formatInr(minor: number | null): string {
  if (minor === null) return "Custom";
  if (minor === 0) return "Free";
  return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(minor / 100);
}
