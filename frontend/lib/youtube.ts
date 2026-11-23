/** YouTube OAuth 2.0 + Data API v3 helpers (server only). */
import type { NextRequest } from "next/server";
import type { YouTubeSession } from "./session";

export const YT_SCOPES = [
  "https://www.googleapis.com/auth/youtube.upload",
  "https://www.googleapis.com/auth/youtube.readonly",
];

export function oauthConfigured(): boolean {
  return Boolean(process.env.GOOGLE_OAUTH_CLIENT_ID && process.env.GOOGLE_OAUTH_CLIENT_SECRET);
}

export function appUrl(req: NextRequest): string {
  if (process.env.APP_URL) return process.env.APP_URL.replace(/\/+$/, "");
  const proto = req.headers.get("x-forwarded-proto") ?? "https";
  const host = req.headers.get("x-forwarded-host") ?? req.headers.get("host") ?? "localhost:3000";
  return `${proto}://${host}`;
}

export function redirectUri(req: NextRequest): string {
  return `${appUrl(req)}/api/youtube/callback`;
}

interface TokenResponse {
  access_token: string;
  expires_in: number;
  refresh_token?: string;
  scope: string;
  token_type: string;
  error?: string;
  error_description?: string;
}

async function tokenRequest(params: Record<string, string>): Promise<TokenResponse> {
  const res = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: process.env.GOOGLE_OAUTH_CLIENT_ID ?? "",
      client_secret: process.env.GOOGLE_OAUTH_CLIENT_SECRET ?? "",
      ...params,
    }),
    cache: "no-store",
  });
  const data = (await res.json()) as TokenResponse;
  if (!res.ok || data.error) {
    throw new Error(data.error_description || data.error || `Token endpoint returned ${res.status}`);
  }
  return data;
}

export async function exchangeCode(code: string, redirect: string): Promise<YouTubeSession> {
  const t = await tokenRequest({ code, grant_type: "authorization_code", redirect_uri: redirect });
  return {
    accessToken: t.access_token,
    refreshToken: t.refresh_token,
    expiresAt: Date.now() + t.expires_in * 1000,
    scope: t.scope,
  };
}

/** Returns a session whose access token is valid for at least 2 more minutes. */
export async function ensureFreshToken(session: YouTubeSession): Promise<{ session: YouTubeSession; refreshed: boolean }> {
  if (session.expiresAt - Date.now() > 120_000) return { session, refreshed: false };
  if (!session.refreshToken) throw new Error("YouTube session expired. Please reconnect your channel.");
  const t = await tokenRequest({ refresh_token: session.refreshToken, grant_type: "refresh_token" });
  return {
    session: {
      ...session,
      accessToken: t.access_token,
      expiresAt: Date.now() + t.expires_in * 1000,
      scope: t.scope || session.scope,
      refreshToken: t.refresh_token ?? session.refreshToken,
    },
    refreshed: true,
  };
}

export async function fetchChannel(accessToken: string): Promise<{ id?: string; title?: string }> {
  const res = await fetch("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true", {
    headers: { Authorization: `Bearer ${accessToken}` },
    cache: "no-store",
  });
  if (!res.ok) return {};
  const data = (await res.json()) as { items?: { id: string; snippet?: { title?: string } }[] };
  const item = data.items?.[0];
  return { id: item?.id, title: item?.snippet?.title };
}

export async function revokeToken(token: string): Promise<void> {
  await fetch(`https://oauth2.googleapis.com/revoke?token=${encodeURIComponent(token)}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  }).catch(() => undefined);
}

/** YouTube rejects < and > in titles/descriptions and enforces length limits. */
export function cleanTitle(title: string): string {
  let t = title.replace(/[<>]/g, "").replace(/\s+/g, " ").trim();
  if (!/#shorts/i.test(t) && t.length <= 91) t = `${t} #Shorts`;
  return t.slice(0, 100) || "My Short #Shorts";
}

export function cleanDescription(description: string): string {
  const d = description.replace(/[<>]/g, "");
  const bytes = new TextEncoder().encode(d);
  if (bytes.length <= 4900) return d;
  return new TextDecoder().decode(bytes.slice(0, 4900)).replace(/�+$/, "");
}

export function cleanTags(tags: string[]): string[] {
  const out: string[] = [];
  let total = 0;
  for (const raw of tags) {
    const tag = raw.replace(/^#/, "").replace(/[<>,"]/g, "").trim().slice(0, 30);
    if (!tag || out.includes(tag)) continue;
    const cost = tag.length + (tag.includes(" ") ? 2 : 0) + (out.length ? 1 : 0);
    if (total + cost > 480) break;
    out.push(tag);
    total += cost;
  }
  return out;
}

/**
 * Two-step resumable upload: create the session with metadata, then PUT the bytes.
 * Returns the new video id.
 */
export async function uploadVideo(opts: {
  accessToken: string;
  video: ArrayBuffer;
  title: string;
  description: string;
  tags: string[];
  privacyStatus: "private" | "unlisted" | "public";
  madeForKids: boolean;
  containsSyntheticMedia: boolean;
}): Promise<string> {
  const metadata = {
    snippet: {
      title: cleanTitle(opts.title),
      description: cleanDescription(opts.description),
      tags: cleanTags(opts.tags),
      categoryId: "22", // People & Blogs
    },
    status: {
      privacyStatus: opts.privacyStatus,
      selfDeclaredMadeForKids: opts.madeForKids,
      containsSyntheticMedia: opts.containsSyntheticMedia,
      embeddable: true,
    },
  };

  const init = await fetch(
    "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${opts.accessToken}`,
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": String(opts.video.byteLength),
      },
      body: JSON.stringify(metadata),
      cache: "no-store",
    },
  );
  if (!init.ok) throw new Error(await youtubeError(init, "Could not start the YouTube upload"));
  const location = init.headers.get("location");
  if (!location) throw new Error("YouTube did not return an upload URL");

  let lastError = "";
  for (let attempt = 0; attempt < 3; attempt++) {
    const put = await fetch(location, {
      method: "PUT",
      headers: { "Content-Type": "video/mp4" },
      body: opts.video,
      cache: "no-store",
    });
    if (put.ok) {
      const data = (await put.json()) as { id?: string };
      if (!data.id) throw new Error("YouTube accepted the upload but returned no video id");
      return data.id;
    }
    lastError = await youtubeError(put, "YouTube upload failed");
    if (put.status < 500) break; // client errors will not succeed on retry
    await new Promise((r) => setTimeout(r, 1500 * (attempt + 1)));
  }
  throw new Error(lastError);
}

async function youtubeError(res: Response, fallback: string): Promise<string> {
  try {
    const data = (await res.json()) as { error?: { message?: string; errors?: { reason?: string }[] } };
    const reason = data.error?.errors?.[0]?.reason;
    if (reason === "quotaExceeded") return "YouTube API daily quota exceeded. Try again tomorrow.";
    if (reason === "uploadLimitExceeded") return "Your channel has reached its upload limit for today.";
    if (reason === "youtubeSignupRequired") return "This Google account has no YouTube channel yet.";
    return data.error?.message ? `${fallback}: ${data.error.message}` : `${fallback} (${res.status})`;
  } catch {
    return `${fallback} (${res.status})`;
  }
}
