import { NextResponse } from "next/server";
import { clearYouTubeSession, readYouTubeSession } from "@/lib/session";
import { revokeToken } from "@/lib/youtube";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST() {
  const session = readYouTubeSession();
  if (session) await revokeToken(session.refreshToken ?? session.accessToken);
  clearYouTubeSession();
  return NextResponse.json({ ok: true });
}
