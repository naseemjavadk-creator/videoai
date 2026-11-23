import { timingSafeEqual } from "node:crypto";
import { NextResponse, type NextRequest } from "next/server";
import { OAUTH_STATE_COOKIE, writeYouTubeSession } from "@/lib/session";
import { appUrl, exchangeCode, fetchChannel, redirectUri } from "@/lib/youtube";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function sameState(a: string, b: string): boolean {
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  return ab.length === bb.length && timingSafeEqual(ab, bb);
}

export async function GET(req: NextRequest) {
  const base = appUrl(req);
  const done = (status: string) => {
    const res = NextResponse.redirect(`${base}/?youtube=${status}`);
    res.cookies.set(OAUTH_STATE_COOKIE, "", { path: "/api/youtube", maxAge: 0 });
    return res;
  };

  const { searchParams } = req.nextUrl;
  if (searchParams.get("error")) return done("denied");

  const code = searchParams.get("code");
  const state = searchParams.get("state");
  const expected = req.cookies.get(OAUTH_STATE_COOKIE)?.value;
  if (!code || !state || !expected || !sameState(state, expected)) return done("invalid_state");

  try {
    const session = await exchangeCode(code, redirectUri(req));
    if (!session.scope.includes("youtube.upload")) return done("missing_scope");
    const channel = await fetchChannel(session.accessToken);
    writeYouTubeSession({ ...session, channelId: channel.id, channelTitle: channel.title });
    return done("connected");
  } catch (err) {
    console.error("YouTube OAuth callback failed", err);
    return done("error");
  }
}
