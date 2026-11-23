/**
 * Password gate for the whole dashboard (Edge runtime, Web Crypto).
 * Keeps strangers from starting jobs that spend your Google Cloud credits.
 */
import { NextResponse, type NextRequest } from "next/server";

const AUTH_COOKIE = "cs_auth";
const PUBLIC_PATHS = ["/login", "/api/login"];

async function hmacHex(secret: string, message: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
    "sign",
  ]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function isAuthenticated(req: NextRequest): Promise<boolean> {
  const secret = process.env.SESSION_SECRET;
  const token = req.cookies.get(AUTH_COOKIE)?.value;
  if (!secret || !token) return false;
  const [expRaw, mac] = token.split(".");
  const exp = Number(expRaw);
  if (!Number.isFinite(exp) || exp * 1000 < Date.now() || !mac) return false;
  const expected = await hmacHex(secret, `auth:${exp}`);
  return safeEqual(mac, expected);
}

export async function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  if (PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`))) {
    return NextResponse.next();
  }
  if (await isAuthenticated(req)) return NextResponse.next();

  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "Please sign in" }, { status: 401 });
  }
  const url = req.nextUrl.clone();
  url.pathname = "/login";
  url.search = pathname === "/" ? "" : `?next=${encodeURIComponent(pathname + req.nextUrl.search)}`;
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.svg|robots.txt).*)"],
};
