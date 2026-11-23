import { NextResponse, type NextRequest } from "next/server";
import { backendJson, errorResponse, BackendError } from "@/lib/backend";
import { clearYouTubeSession, readYouTubeSession, writeYouTubeSession } from "@/lib/session";
import type { UploadInput, UploadResult } from "@/lib/types";
import { ensureFreshToken, oauthConfigured, uploadVideo } from "@/lib/youtube";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 300; // seconds (Vercel); Cloud Run allows the 300 s request timeout set in deploy.sh

const MAX_BYTES = 512 * 1024 * 1024;

interface SignedClip {
  url: string;
  expires_at: string;
  content_type: string;
  size_bytes: number;
  title: string;
  description: string;
  hashtags: string[];
}

export async function POST(req: NextRequest) {
  if (!oauthConfigured()) {
    return NextResponse.json({ error: "YouTube upload is not configured on this server." }, { status: 400 });
  }
  const stored = readYouTubeSession();
  if (!stored) {
    return NextResponse.json({ error: "Connect your YouTube channel first." }, { status: 401 });
  }

  let input: UploadInput;
  try {
    input = (await req.json()) as UploadInput;
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  if (!/^[a-zA-Z0-9]{1,32}$/.test(input.jobId ?? "") || !Number.isInteger(input.index) || input.index < 0) {
    return NextResponse.json({ error: "Invalid clip reference" }, { status: 400 });
  }
  if (!["private", "unlisted", "public"].includes(input.privacyStatus)) {
    return NextResponse.json({ error: "Invalid privacy setting" }, { status: 400 });
  }

  let session = stored;
  try {
    const fresh = await ensureFreshToken(stored);
    session = fresh.session;
    if (fresh.refreshed) writeYouTubeSession(session);
  } catch (err) {
    clearYouTubeSession();
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "YouTube session expired. Please reconnect." },
      { status: 401 },
    );
  }

  let clip: SignedClip;
  try {
    clip = await backendJson<SignedClip>(`/api/jobs/${input.jobId}/clips/${input.index}/url`);
  } catch (err) {
    return errorResponse(err);
  }
  if (clip.size_bytes > MAX_BYTES) {
    return NextResponse.json({ error: "This clip is too large to upload from the dashboard." }, { status: 413 });
  }

  try {
    const media = await fetch(clip.url, { cache: "no-store", signal: AbortSignal.timeout(120_000) });
    if (!media.ok) throw new BackendError(502, `Could not read the rendered clip (${media.status})`);
    const video = await media.arrayBuffer();

    const videoId = await uploadVideo({
      accessToken: session.accessToken,
      video,
      title: input.title || clip.title,
      description: input.description || clip.description,
      tags: input.tags?.length ? input.tags : clip.hashtags,
      privacyStatus: input.privacyStatus,
      madeForKids: Boolean(input.madeForKids),
      containsSyntheticMedia: Boolean(input.containsSyntheticMedia),
    });
    const result: UploadResult = { videoId, url: `https://youtube.com/shorts/${videoId}` };
    return NextResponse.json(result);
  } catch (err) {
    if (err instanceof BackendError) return errorResponse(err);
    const message = err instanceof Error ? err.message : "YouTube upload failed";
    console.error("YouTube upload failed", err);
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
