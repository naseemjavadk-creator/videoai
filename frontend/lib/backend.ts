/**
 * Server-side client for the private FastAPI backend.
 *
 * - On Cloud Run (K_SERVICE is set) it adds a Google-signed ID token from the
 *   metadata server, which satisfies the backend's run.invoker IAM check.
 * - Everywhere (Cloud Run, Vercel, local) it adds the shared X-API-Key header.
 */

const ID_TOKEN_TTL_MS = 50 * 60 * 1000;
let cachedIdToken: { token: string; audience: string; expiresAt: number } | null = null;

function backendUrl(): string {
  const url = process.env.BACKEND_URL;
  if (!url) throw new Error("BACKEND_URL is not configured");
  return url.replace(/\/+$/, "");
}

async function getIdToken(audience: string): Promise<string | null> {
  if (!process.env.K_SERVICE) return null; // not on Cloud Run
  if (cachedIdToken && cachedIdToken.audience === audience && cachedIdToken.expiresAt > Date.now()) {
    return cachedIdToken.token;
  }
  const res = await fetch(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity" +
      `?audience=${encodeURIComponent(audience)}&format=full`,
    { headers: { "Metadata-Flavor": "Google" }, cache: "no-store" },
  );
  if (!res.ok) throw new Error(`Metadata server returned ${res.status} for ID token`);
  const token = (await res.text()).trim();
  cachedIdToken = { token, audience, expiresAt: Date.now() + ID_TOKEN_TTL_MS };
  return token;
}

export class BackendError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function backendFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const base = backendUrl();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (process.env.BACKEND_API_KEY) headers.set("X-API-Key", process.env.BACKEND_API_KEY);
  const idToken = await getIdToken(base);
  if (idToken) headers.set("Authorization", `Bearer ${idToken}`);

  return fetch(`${base}${path}`, {
    ...init,
    headers,
    cache: "no-store",
    signal: init.signal ?? AbortSignal.timeout(60_000),
  });
}

export async function backendJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await backendFetch(path, init);
  } catch (err) {
    console.error("backend unreachable", err);
    throw new BackendError(502, "The processing server is unreachable. Please try again shortly.");
  }
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? formatDetail((data as { detail: unknown }).detail)
        : `Backend error ${res.status}`;
    throw new BackendError(res.status, detail);
  }
  return data as T;
}

function formatDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (d && typeof d === "object" && "msg" in d) {
          const loc = Array.isArray((d as { loc?: unknown[] }).loc) ? (d as { loc: unknown[] }).loc.slice(1).join(".") : "";
          return `${loc ? `${loc}: ` : ""}${String((d as { msg: unknown }).msg)}`;
        }
        return String(d);
      })
      .join("; ");
  }
  return "Request failed";
}

export function errorResponse(err: unknown): Response {
  if (err instanceof BackendError) {
    return Response.json({ error: err.message }, { status: err.status });
  }
  console.error(err);
  return Response.json({ error: "Unexpected server error" }, { status: 500 });
}
