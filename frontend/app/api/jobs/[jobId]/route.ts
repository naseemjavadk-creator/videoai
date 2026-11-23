import { NextResponse } from "next/server";
import { backendJson, errorResponse } from "@/lib/backend";
import type { Job } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function validId(id: string): boolean {
  return /^[a-zA-Z0-9]{1,32}$/.test(id);
}

export async function GET(_req: Request, { params }: { params: { jobId: string } }) {
  if (!validId(params.jobId)) return NextResponse.json({ error: "Job not found" }, { status: 404 });
  try {
    const job = await backendJson<Job>(`/api/jobs/${params.jobId}`);
    return NextResponse.json(job);
  } catch (err) {
    return errorResponse(err);
  }
}

export async function POST(_req: Request, { params }: { params: { jobId: string } }) {
  // Retry: queue a new job with the same settings.
  if (!validId(params.jobId)) return NextResponse.json({ error: "Job not found" }, { status: 404 });
  try {
    const out = await backendJson<{ job_id: string }>(`/api/jobs/${params.jobId}/retry`, { method: "POST" });
    return NextResponse.json(out, { status: 202 });
  } catch (err) {
    return errorResponse(err);
  }
}

export async function DELETE(_req: Request, { params }: { params: { jobId: string } }) {
  if (!validId(params.jobId)) return NextResponse.json({ error: "Job not found" }, { status: 404 });
  try {
    const out = await backendJson<{ job_id: string; deleted_objects: number }>(`/api/jobs/${params.jobId}`, {
      method: "DELETE",
    });
    return NextResponse.json(out);
  } catch (err) {
    return errorResponse(err);
  }
}
