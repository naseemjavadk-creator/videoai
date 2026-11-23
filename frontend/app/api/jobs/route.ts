import { NextResponse, type NextRequest } from "next/server";
import { backendJson, errorResponse } from "@/lib/backend";
import type { CreateJobInput, JobSummary } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const jobs = await backendJson<JobSummary[]>("/api/jobs?limit=20");
    return NextResponse.json(jobs);
  } catch (err) {
    return errorResponse(err);
  }
}

export async function POST(req: NextRequest) {
  let input: CreateJobInput;
  try {
    input = (await req.json()) as CreateJobInput;
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  if (!input?.url || typeof input.url !== "string") {
    return NextResponse.json({ error: "Paste a YouTube URL" }, { status: 400 });
  }
  if (input.rights_confirmed !== true) {
    return NextResponse.json(
      { error: "Confirm that you own this video or have the rights to clip it." },
      { status: 400 },
    );
  }
  try {
    const created = await backendJson<{ job_id: string; status: string; position: number }>("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        url: input.url.trim(),
        max_clips: input.max_clips,
        min_clip_seconds: input.min_clip_seconds,
        max_clip_seconds: input.max_clip_seconds,
        language_code: input.language_code,
        tts_voice: input.tts_voice || null,
        commentary_style: input.commentary_style,
        caption_color: input.caption_color,
        rights_confirmed: true,
      }),
    });
    return NextResponse.json(created, { status: 202 });
  } catch (err) {
    return errorResponse(err);
  }
}
