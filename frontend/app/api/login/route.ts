import { NextResponse, type NextRequest } from "next/server";
import { checkPassword, clearAuthCookie, setAuthCookie } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Simple in-memory throttle per IP (per instance) against password guessing.
const attempts = new Map<string, { count: number; resetAt: number }>();

function throttled(ip: string): boolean {
  const now = Date.now();
  const entry = attempts.get(ip);
  if (!entry || entry.resetAt < now) {
    attempts.set(ip, { count: 1, resetAt: now + 15 * 60_000 });
    return false;
  }
  entry.count += 1;
  return entry.count > 10;
}

export async function POST(req: NextRequest) {
  if (!process.env.APP_PASSWORD) {
    return NextResponse.json({ error: "APP_PASSWORD is not configured on the server." }, { status: 500 });
  }
  const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";
  if (throttled(ip)) {
    return NextResponse.json({ error: "Too many attempts. Wait 15 minutes and try again." }, { status: 429 });
  }
  const body = (await req.json().catch(() => ({}))) as { password?: unknown };
  const password = typeof body.password === "string" ? body.password : "";
  if (!password || !checkPassword(password)) {
    await new Promise((r) => setTimeout(r, 600));
    return NextResponse.json({ error: "Wrong password" }, { status: 401 });
  }
  attempts.delete(ip);
  setAuthCookie();
  return NextResponse.json({ ok: true });
}

export async function DELETE() {
  clearAuthCookie();
  return NextResponse.json({ ok: true });
}
