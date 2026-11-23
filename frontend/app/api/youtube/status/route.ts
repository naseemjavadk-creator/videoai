import { NextResponse } from "next/server";
import { readYouTubeSession } from "@/lib/session";
import { oauthConfigured } from "@/lib/youtube";
import type { YouTubeStatus } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const configured = oauthConfigured();
  const session = configured ? readYouTubeSession() : null;
  const body: YouTubeStatus = {
    configured,
    connected: Boolean(session && (session.refreshToken || session.expiresAt > Date.now())),
    channelTitle: session?.channelTitle,
    channelId: session?.channelId,
  };
  return NextResponse.json(body);
}
