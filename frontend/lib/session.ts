/**
 * Encrypted, httpOnly cookie sessions (Node.js runtime only).
 *
 * The YouTube OAuth tokens never reach the browser in readable form: they are sealed
 * with AES-256-GCM using a key derived from SESSION_SECRET.
 */
import { createCipheriv, createDecipheriv, createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";

export const AUTH_COOKIE = "cs_auth";
export const YT_COOKIE = "cs_yt";
export const OAUTH_STATE_COOKIE = "cs_oauth_state";

const AUTH_TTL_SECONDS = 60 * 60 * 24 * 14;
const YT_TTL_SECONDS = 60 * 60 * 24 * 30;

function secret(): string {
  const s = process.env.SESSION_SECRET;
  if (!s || s.length < 32) throw new Error("SESSION_SECRET must be set (32+ characters)");
  return s;
}

function key(): Buffer {
  return createHash("sha256").update(`enc:${secret()}`).digest();
}

export function seal(value: unknown): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key(), iv);
  const data = Buffer.concat([cipher.update(JSON.stringify(value), "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, data]).toString("base64url");
}

export function unseal<T>(token: string | undefined): T | null {
  if (!token) return null;
  try {
    const raw = Buffer.from(token, "base64url");
    const iv = raw.subarray(0, 12);
    const tag = raw.subarray(12, 28);
    const data = raw.subarray(28);
    const decipher = createDecipheriv("aes-256-gcm", key(), iv);
    decipher.setAuthTag(tag);
    const plain = Buffer.concat([decipher.update(data), decipher.final()]).toString("utf8");
    return JSON.parse(plain) as T;
  } catch {
    return null;
  }
}

/** Same algorithm as middleware.ts (Web Crypto): HMAC-SHA256(secret, "auth:" + expiry). */
export function authToken(expiresAtSeconds: number): string {
  const mac = createHmac("sha256", secret()).update(`auth:${expiresAtSeconds}`).digest("hex");
  return `${expiresAtSeconds}.${mac}`;
}

export function checkPassword(candidate: string): boolean {
  const expected = process.env.APP_PASSWORD;
  if (!expected) return false;
  const a = createHash("sha256").update(candidate).digest();
  const b = createHash("sha256").update(expected).digest();
  return timingSafeEqual(a, b);
}

export function isSecureContext(): boolean {
  return process.env.NODE_ENV === "production";
}

export function setAuthCookie(): void {
  const exp = Math.floor(Date.now() / 1000) + AUTH_TTL_SECONDS;
  cookies().set(AUTH_COOKIE, authToken(exp), {
    httpOnly: true,
    secure: isSecureContext(),
    sameSite: "lax",
    path: "/",
    maxAge: AUTH_TTL_SECONDS,
  });
}

export function clearAuthCookie(): void {
  cookies().delete(AUTH_COOKIE);
}

export interface YouTubeSession {
  accessToken: string;
  refreshToken?: string;
  expiresAt: number; // epoch ms
  scope: string;
  channelId?: string;
  channelTitle?: string;
}

export function readYouTubeSession(): YouTubeSession | null {
  return unseal<YouTubeSession>(cookies().get(YT_COOKIE)?.value);
}

export function writeYouTubeSession(session: YouTubeSession): void {
  cookies().set(YT_COOKIE, seal(session), {
    httpOnly: true,
    secure: isSecureContext(),
    sameSite: "lax",
    path: "/",
    maxAge: YT_TTL_SECONDS,
  });
}

export function clearYouTubeSession(): void {
  cookies().delete(YT_COOKIE);
}
