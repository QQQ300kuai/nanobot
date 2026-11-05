import { fetchWithTimeout } from "./http";

const TOKEN_STORAGE_KEY = "nanobot-webui.account-token";
const TIMEOUT_MS = 20_000;

export class AccountApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string) {
    super(code);
    this.name = "AccountApiError";
    this.status = status;
    this.code = code;
  }
}

export interface AccountIdentity {
  userId: string;
  isAdmin: boolean;
}

export interface McpCatalogParam {
  name: string;
  secret: boolean;
  description: string;
  required: boolean;
  set: boolean;
  value?: string;
}

export interface McpCatalogEntry {
  name: string;
  kind: "mcp" | "plugin" | string;
  description: string;
  enabled: boolean;
  params: McpCatalogParam[];
}

export interface UsageSummary {
  total_tokens_30d: number;
  requests_30d: number;
  failed_requests_30d: number;
  cache_read_rate_30d: number | null;
  providers_30d: Array<{
    provider: string;
    model: string;
    total_tokens: number;
    requests: number;
  }>;
  users_30d?: Array<{
    user_id: string | null;
    total_tokens: number;
    requests: number;
    failed_requests: number;
  }>;
}

/** The account token lives in sessionStorage so it ends with the browser tab. */
export function loadAccountToken(): string {
  try {
    return window.sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveAccountToken(token: string): void {
  try {
    window.sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    // ignore storage errors (private mode, etc.)
  }
}

export function clearAccountToken(): void {
  try {
    window.sessionStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // ignore
  }
}

async function call<T>(
  path: string,
  token: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body) headers["Content-Type"] = "application/json";
  const res = await fetchWithTimeout(path, {
    method: body ? "POST" : "GET",
    credentials: "same-origin",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  }, TIMEOUT_MS);
  if (!res.ok) {
    let code = `HTTP ${res.status}`;
    try {
      const payload = (await res.json()) as { error?: unknown };
      if (typeof payload.error === "string" && payload.error.trim()) code = payload.error.trim();
    } catch {
      // keep the status text
    }
    throw new AccountApiError(res.status, code);
  }
  return (await res.json()) as T;
}

export async function loginAccount(userId: string, password: string): Promise<string> {
  const { token } = await call<{ token: string }>("/auth/login", "", { user_id: userId, password });
  return token;
}

/** Returns the replacement token: a password change revokes every older one. */
export async function changeAccountPassword(
  token: string,
  oldPassword: string,
  newPassword: string,
): Promise<string> {
  const result = await call<{ token: string }>("/auth/password", token, {
    old: oldPassword,
    new: newPassword,
  });
  return result.token;
}

export async function fetchWsTicket(token: string): Promise<string> {
  const { ticket } = await call<{ ticket: string }>("/auth/ws-ticket", token, {});
  return ticket;
}

export async function fetchMcpCatalog(token: string): Promise<McpCatalogEntry[]> {
  return (await call<{ entries: McpCatalogEntry[] }>("/api/mcp/catalog", token)).entries;
}

export async function setMcpCatalogEnabled(
  token: string,
  name: string,
  enabled: boolean,
): Promise<McpCatalogEntry[]> {
  return (await call<{ entries: McpCatalogEntry[] }>(
    "/api/mcp/catalog/enable", token, { name, enabled },
  )).entries;
}

export async function setMcpCatalogParams(
  token: string,
  name: string,
  params: Record<string, string>,
): Promise<McpCatalogEntry[]> {
  return (await call<{ entries: McpCatalogEntry[] }>(
    "/api/mcp/catalog/params", token, { name, params },
  )).entries;
}

export function fetchOwnUsage(token: string): Promise<UsageSummary> {
  return call<UsageSummary>("/api/usage", token);
}

export function fetchAdminUsage(token: string): Promise<UsageSummary> {
  return call<UsageSummary>("/api/admin/usage", token);
}
