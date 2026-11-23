import { randomBytes } from "node:crypto";
import { NextResponse, type NextRequest } from "next/server";
import { OAUTH_STATE_COOKIE, isSecureContext } from "@/lib/session";
import { YT_SCOPES, appUrl, oauthConfigured, redirectUri } from "@/lib/youtube";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!oauthConfigured()) {
    return NextResponse.redirect(`${appUrl(req)}/?youtube=not_configured`);
  }
  const state = randomBytes(24).toString("base64url");
  const params = new URLSearchParams({
    client_id: process.env.GOOGLE_OAUTH_CLIENT_ID ?? "",
    redirect_uri: redirectUri(req),
    response_type: "code",
    scope: YT_SCOPES.join(" "),
    access_type: "offline",
    prompt: "consent select_account",
    include_granted_scopes: "true",
    state,
  });
  const res = NextResponse.redirect(`https://accounts.google.com/o/oauth2/v2/auth?${params.toString()}`);
  res.cookies.set(OAUTH_STATE_COOKIE, state, {
    httpOnly: true,
    secure: isSecureContext(),
    sameSite: "lax",
    path: "/api/youtube",
    maxAge: 600,
  });
  return res;
}
