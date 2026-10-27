import { createClient } from "@/utils/supabase/client";

const API = process.env.NEXT_PUBLIC_API_URL!;
/** Local demo: no Supabase/R2/Anthropic. The backend accepts Bearer demo. */
export const DEMO_MODE = process.env.NEXT_PUBLIC_DEMO_MODE === "true";

/** Token for direct fetch() calls that upload files. */
export async function accessToken(): Promise<string | null> {
  if (DEMO_MODE) return "demo";
  const { data } = await createClient().auth.getSession();
  return data.session?.access_token ?? null;
}
/** Server components (SSR) must reach the API over the container network.
 * NEXT_PUBLIC_* is baked for the browser, which is why the passport page
 * used to fail with ECONNREFUSED inside Docker. */
const SERVER_API = process.env.API_URL_INTERNAL || API;

export function supabaseBrowser() {
  return createClient();
}

/** Authenticated API call from the reviewer console (Supabase session token). */
export async function apiAuthed(
  path: string,
  init: RequestInit = {},
  timeoutMs = 15_000,
) {
  const supabase = createClient();

  const getToken = async (): Promise<string | null> => {
    const { data } = await supabase.auth.getSession();
    return data.session?.access_token ?? null;
  };

  const truncate = (body: string, max = 300): string =>
    body.length > max ? `${body.slice(0, max)}…` : body;

  const doFetch = async (token: string, signal: AbortSignal) => {
    const res = await fetch(`${API}${path}`, {
      ...init,
      signal,
      headers: { ...(init.headers || {}), Authorization: `Bearer ${token}` },
    });
    return res;
  };

  const throwForStatus = async (res: Response): Promise<never> => {
    if (res.status === 401) throw new Error("SESSION_EXPIRED");
    let body = "";
    try {
      body = await res.text();
    } catch {
      body = "";
    }
    throw new Error(`Request failed: ${res.status}${body ? ` ${truncate(body)}` : ""}`);
  };

  const withTimeout = async <T>(fn: (signal: AbortSignal) => Promise<T>): Promise<T> => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fn(controller.signal);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  };

  if (DEMO_MODE) {
    const res = await withTimeout((signal) => doFetch("demo", signal));
    if (res.ok) return res.json();
    await throwForStatus(res);
  }

  let token = await getToken();
  if (!token) {
    // Session may be expired but refreshable — try once before giving up.
    try {
      const { data } = await supabase.auth.refreshSession();
      token = data.session?.access_token ?? null;
    } catch {
      token = null;
    }
  }
  if (!token) throw new Error("SESSION_EXPIRED");

  try {
    const res = await withTimeout((signal) => doFetch(token as string, signal));
    if (res.ok) return res.json();
    if (res.status !== 401) await throwForStatus(res);
    // 401: access token may be stale — refresh once and retry once.
    try {
      await res.body?.cancel().catch(() => { });
    } catch {
      // ignore body-cancel errors before retry
    }
    const { data: refreshed } = await supabase.auth.refreshSession();
    const retryToken = refreshed.session?.access_token;
    if (!retryToken) throw new Error("SESSION_EXPIRED");
    const retryRes = await withTimeout((signal) => doFetch(retryToken, signal));
    if (retryRes.ok) return retryRes.json();
    await throwForStatus(retryRes);
  } catch (err) {
    // Preserve our intentional sentinel + status errors without wrapping.
    if (err instanceof Error) throw err;
    throw new Error("Request failed");
  }
  // Unreachable — throwForStatus always throws.
  throw new Error("Request failed");
}

export type PublicPassport = {
  product_name: string;
  sku?: string | null;
  manufacturer_name?: string | null;
  category?: string | null;
  published_at?: string | null;
  fields: { field_key?: string; label: string; value: string; unit: string | null }[];
};

/** Public passport fetch: NO auth header, hits the isolated public route only. */
export async function apiPublicPassport(slug: string) {
  const res = await fetch(`${SERVER_API}/api/public/passport/${slug}`, { cache: "no-store" });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Passport fetch failed: ${res.status}`);
  return res.json() as Promise<PublicPassport>;
}

/** Public signup pre-check: does this company code still have seats? No auth. */
export async function apiCheckInvite(code: string) {
  const res = await fetch(`${API}/api/invites/check`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
    cache: "no-store",
  });
  if (!res.ok) {
    let message = `Invite check failed: ${res.status}`;
    try {
      const body = await res.json();
      message = body?.detail?.message || body?.detail || message;
    } catch {
      // keep fallback
    }
    throw new Error(typeof message === "string" ? message : "Invalid invite code");
  }
  return res.json() as Promise<{ ok: boolean; seats_left: number | null }>;
}

export type FounderInvite = {
  id: string;
  role: string;
  used_count: number;
  max_uses: number | null;
  expires_at: string | null;
  created_at: string | null;
  company_name: string;
  contact_name: string;
  contact_email: string;
};

/** Founder reporting, key-gated. The key lives in sessionStorage only. */
export async function apiFounderList(founderKey: string) {
  const res = await fetch(`${API}/api/founder/invites`, {
    headers: { "X-Founder-Key": founderKey },
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`Founder list failed: ${res.status}`);
  return res.json() as Promise<{ items: FounderInvite[] }>;
}

export type FounderMintInput = {
  company_name: string;
  contact_name?: string;
  contact_email?: string;
  seats?: number;
  days?: number;
  prefix?: string;
};

/** Mint one company code. Plaintext is returned exactly once. */
export async function apiFounderMint(founderKey: string, input: FounderMintInput) {
  const res = await fetch(`${API}/api/founder/invites`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Founder-Key": founderKey },
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    let message = `Mint failed: ${res.status}`;
    try {
      const body = await res.json();
      message = body?.detail?.message || body?.detail || message;
    } catch {
      // keep fallback
    }
    throw new Error(typeof message === "string" ? message : "Mint failed");
  }
  return res.json() as Promise<{ code: string; company: string; seats: number; days: number; expires_at: string | null }>;
}

/** Redeem result: workspace membership created for the signed-in user. */
export type InviteRedeemResult = {
  workspace: string;
  role: string;
};

/** Redeem an invite code for the signed-in user (Supabase session token).
 * Mirrors apiAuthed: 15s timeout, 401 refresh-once, truncated errors. */
export async function apiRedeemInvite(code: string): Promise<InviteRedeemResult> {
  if (DEMO_MODE) {
    return { workspace: "demo-workspace", role: "reviewer" } as InviteRedeemResult;
  }
  const timeoutMs = 15_000;
  const supabase = createClient();

  const getToken = async (): Promise<string | null> => {
    const { data } = await supabase.auth.getSession();
    return data.session?.access_token ?? null;
  };

  const truncate = (body: string, max = 300): string =>
    body.length > max ? `${body.slice(0, max)}…` : body;

  const doFetch = async (token: string, signal: AbortSignal) => {
    const res = await fetch(`${API}/api/invites/redeem`, {
      method: "POST",
      signal,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    return res;
  };

  const throwForStatus = async (res: Response): Promise<never> => {
    if (res.status === 401) throw new Error("SESSION_EXPIRED");
    let body = "";
    try {
      body = await res.text();
    } catch {
      body = "";
    }
    throw new Error(`Request failed: ${res.status}${body ? ` ${truncate(body)}` : ""}`);
  };

  const withTimeout = async <T>(fn: (signal: AbortSignal) => Promise<T>): Promise<T> => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fn(controller.signal);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  };

  if (DEMO_MODE) {
    const res = await withTimeout((signal) => doFetch("demo", signal));
    if (res.ok) return res.json();
    await throwForStatus(res);
  }

  let token = await getToken();
  if (!token) {
    try {
      const { data } = await supabase.auth.refreshSession();
      token = data.session?.access_token ?? null;
    } catch {
      token = null;
    }
  }
  if (!token) throw new Error("SESSION_EXPIRED");

  const parseResult = (data: unknown): InviteRedeemResult => {
    const rec = (data ?? {}) as Record<string, unknown>;
    const workspace = String(rec.workspace ?? rec.workspace_id ?? rec.workspace_name ?? "");
    const role = String(rec.role ?? "");
    return { workspace, role };
  };

  try {
    const res = await withTimeout((signal) => doFetch(token as string, signal));
    if (res.ok) return parseResult(await res.json());
    if (res.status !== 401) await throwForStatus(res);
    try {
      await res.body?.cancel().catch(() => { });
    } catch {
      // ignore body-cancel errors before retry
    }
    const { data: refreshed } = await supabase.auth.refreshSession();
    const retryToken = refreshed.session?.access_token;
    if (!retryToken) throw new Error("SESSION_EXPIRED");
    const retryRes = await withTimeout((signal) => doFetch(retryToken, signal));
    if (retryRes.ok) return parseResult(await retryRes.json());
    await throwForStatus(retryRes);
  } catch (err) {
    if (err instanceof Error) throw err;
    throw new Error("Request failed");
  }
  throw new Error("Request failed");
}

/** Notification center: list (newest first), with unread count. */
export async function apiNotifications(unreadOnly = false, limit = 50) {
  const q = `?unread_only=${unreadOnly ? "true" : "false"}&limit=${Math.max(1, Math.min(limit, 100))}`;
  return apiAuthed(`/api/notifications${q}`) as Promise<{
    items: { id: string; type: string; title: string; body: string | null; entity_type: string | null; entity_id: string | null; read_at: string | null; created_at: string | null }[];
    unread_count: number;
  }>;
}

/** Mark one notification read. */
export async function apiMarkRead(id: string) {
  return apiAuthed(`/api/notifications/${id}/read`, { method: "POST" });
}

/** Mark all notifications read. */
export async function apiReadAll() {
  return apiAuthed("/api/notifications/read-all", { method: "POST" }) as Promise<{ ok: boolean; marked: number }>;
}

/** Email-ready intake: paste subject + body, itemize requirements, notify manager. */
export async function apiIntake(subject: string, body_text: string, from_email?: string) {
  return apiAuthed("/api/intake", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subject, body_text, from_email }),
  }) as Promise<{ request_id: string; items_count: number }>;
}

/** Extract a human message from the API's {detail:{code,message,hint}} shape. */
function apiErrorMessage(data: unknown, status: number): string {
  const rec = (data ?? {}) as Record<string, any>;
  const detail = rec.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    if (detail.message) return String(detail.message);
    if (detail.code) return String(detail.code);
  }
  if (rec.message) return String(rec.message);
  return `Request failed: ${status}`;
}

const MULTIPART_LIMIT = 4 * 1024 * 1024;

/**
 * Upload one document. Large files use a presigned R2 PUT so Vercel's ~4.5 MB
 * request-body limit does not apply; smaller files use the normal multipart
 * endpoint. If direct upload is not configured, it falls back automatically.
 */
export async function apiUploadDocument(
  productId: string,
  file: File,
  supplierId?: string,
): Promise<{ document_id: string; status: string }> {
  const token = await accessToken();
  if (!token) throw new Error("SESSION_EXPIRED");
  const base = `${API}/api/products/${productId}/documents`;

  if (file.size > MULTIPART_LIMIT) {
    let presignFallback = true;
    try {
      const presign = await fetch(`${base}/presign`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, size: file.size }),
      });
      const pdata = await presign.json().catch(() => null);
      if (!presign.ok) {
        const code = (pdata as any)?.detail?.code;
        if (code !== "presign_unavailable") {
          throw new Error(apiErrorMessage(pdata, presign.status));
        }
      } else {
        presignFallback = false;
        const p = pdata as { upload_url: string; storage_key: string; content_type: string };
        const put = await fetch(p.upload_url, {
          method: "PUT",
          headers: { "Content-Type": p.content_type },
          body: file,
        });
        if (!put.ok) throw new Error(`Direct upload failed (${put.status}). Check the R2 bucket CORS rules.`);
        const complete = await fetch(`${base}/complete`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify({
            filename: file.name,
            storage_key: p.storage_key,
            supplier_id: supplierId ?? null,
          }),
        });
        const cdata = await complete.json().catch(() => null);
        if (!complete.ok) throw new Error(apiErrorMessage(cdata, complete.status));
        return cdata as { document_id: string; status: string };
      }
    } catch (err) {
      if (!presignFallback) throw err;
      // presign unavailable: fall through to multipart
    }
  }

  const fd = new FormData();
  fd.append("file", file);
  if (supplierId) fd.append("supplier_id", supplierId);
  let res: Response;
  try {
    res = await fetch(base, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: fd,
    });
  } catch (err) {
    if (file.size > MULTIPART_LIMIT) {
      throw new Error(
        "This file is too large for the standard upload path, and direct-to-storage upload is not configured.",
      );
    }
    throw err;
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const message = apiErrorMessage(data, res.status);
    if (res.status === 413) {
      throw new Error(
        `${message} This file is too large for the standard upload path; direct-to-storage upload is not configured.`,
      );
    }
    throw new Error(message);
  }
  return data as { document_id: string; status: string };
}
