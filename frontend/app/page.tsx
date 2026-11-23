"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Captions,
  CheckCircle2,
  Clapperboard,
  Download as DownloadIcon,
  Film,
  History,
  ImageIcon,
  Link2,
  Loader2,
  LogOut,
  Mic,
  RefreshCw,
  ScanFace,
  ShieldCheck,
  Sparkles,
  Trash2,
  Unplug,
  Wand2,
  XCircle,
} from "lucide-react";
import VideoPreviewCard from "@/components/VideoPreviewCard";
import type { CreateJobInput, Job, JobSummary, YouTubeStatus } from "@/lib/types";

const LANGUAGES = [
  { code: "en-US", label: "English (US)" },
  { code: "en-IN", label: "English (India)" },
  { code: "ml-IN", label: "Malayalam" },
  { code: "hi-IN", label: "Hindi" },
  { code: "ta-IN", label: "Tamil" },
  { code: "es-US", label: "Spanish (US)" },
];

const VOICES = [
  { id: "Charon", label: "Charon · warm male" },
  { id: "Puck", label: "Puck · upbeat male" },
  { id: "Fenrir", label: "Fenrir · bold male" },
  { id: "Kore", label: "Kore · clear female" },
  { id: "Aoede", label: "Aoede · bright female" },
  { id: "Leda", label: "Leda · calm female" },
];

const LENGTHS = [
  { id: "short", label: "15–30 s", min: 15, max: 30 },
  { id: "medium", label: "20–50 s", min: 20, max: 50 },
  { id: "long", label: "40–90 s", min: 40, max: 90 },
];

const CAPTION_COLORS = ["#FFE14D", "#22D3EE", "#F472B6", "#A3E635", "#FFFFFF"];

const STAGES = [
  { key: "download", label: "Download", icon: DownloadIcon },
  { key: "analyze", label: "Gemini picks", icon: Sparkles },
  { key: "render", label: "Transform", icon: Wand2 },
  { key: "done", label: "Ready", icon: CheckCircle2 },
];

const YT_MESSAGES: Record<string, string> = {
  connected: "YouTube channel connected.",
  denied: "YouTube access was not granted.",
  invalid_state: "The YouTube sign-in expired. Please try again.",
  missing_scope: "Upload permission was not granted. Please allow it when connecting.",
  not_configured: "YouTube upload is not configured on the server yet.",
  error: "Could not connect YouTube. Please try again.",
};

function stageIndex(stage: string, status: string): number {
  if (status === "succeeded") return 3;
  if (stage === "render") return 2;
  if (stage === "analyze") return 1;
  return 0;
}

function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, cache: "no-store" });
  if (res.status === 401 && path.startsWith("/api/jobs")) {
    window.location.href = "/login";
  }
  const data = (await res.json().catch(() => ({}))) as T & { error?: string };
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

export default function Dashboard() {
  // Form
  const [url, setUrl] = useState("");
  const [maxClips, setMaxClips] = useState(3);
  const [length, setLength] = useState("medium");
  const [language, setLanguage] = useState("en-US");
  const [voice, setVoice] = useState("Charon");
  const [style, setStyle] = useState("energetic, insightful creator");
  const [captionColor, setCaptionColor] = useState(CAPTION_COLORS[0]);
  const [rights, setRights] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Data
  const [job, setJob] = useState<Job | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [history, setHistory] = useState<JobSummary[]>([]);
  const [youtube, setYoutube] = useState<YouTubeStatus>({ configured: false, connected: false });
  const [banner, setBanner] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadHistory = useCallback(async () => {
    try {
      setHistory(await api<JobSummary[]>("/api/jobs"));
    } catch {
      /* history is optional */
    }
  }, []);

  const loadYouTube = useCallback(async () => {
    try {
      setYoutube(await api<YouTubeStatus>("/api/youtube/status"));
    } catch {
      setYoutube({ configured: false, connected: false });
    }
  }, []);

  // Initial load: restore ?job= and show OAuth result banner.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get("job");
    const yt = params.get("youtube");
    if (fromUrl && /^[a-zA-Z0-9]{1,32}$/.test(fromUrl)) setJobId(fromUrl);
    if (yt && YT_MESSAGES[yt]) setBanner(YT_MESSAGES[yt]);
    if (yt) {
      params.delete("youtube");
      const q = params.toString();
      window.history.replaceState(null, "", q ? `/?${q}` : "/");
    }
    void loadHistory();
    void loadYouTube();
  }, [loadHistory, loadYouTube]);

  // Poll the selected job until it finishes.
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;

    const tick = async () => {
      try {
        const j = await api<Job>(`/api/jobs/${jobId}`);
        if (cancelled) return;
        setJob(j);
        if (j.status === "queued" || j.status === "running") {
          pollRef.current = setTimeout(tick, 3000);
        } else {
          void loadHistory();
        }
      } catch (err) {
        if (cancelled) return;
        setJob(null);
        setFormError(err instanceof Error ? err.message : "Could not load the job");
      }
    };
    void tick();
    return () => {
      cancelled = true;
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [jobId, loadHistory]);

  function selectJob(id: string) {
    setFormError(null);
    setJob(null);
    setJobId(id);
    const params = new URLSearchParams(window.location.search);
    params.set("job", id);
    window.history.replaceState(null, "", `/?${params.toString()}`);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!rights) {
      setFormError("Please confirm you own this video or have the rights to clip it.");
      return;
    }
    const range = LENGTHS.find((l) => l.id === length) ?? LENGTHS[1];
    const body: CreateJobInput = {
      url: url.trim(),
      max_clips: maxClips,
      min_clip_seconds: range.min,
      max_clip_seconds: range.max,
      language_code: language,
      tts_voice: `${language}-Chirp3-HD-${voice}`,
      commentary_style: style.trim() || "energetic, insightful creator",
      caption_color: captionColor,
      rights_confirmed: true,
    };
    setSubmitting(true);
    try {
      const created = await api<{ job_id: string }>("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      selectJob(created.job_id);
      setUrl("");
      void loadHistory();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Could not start the job");
    } finally {
      setSubmitting(false);
    }
  }

  async function cancelOrDelete(id: string) {
    try {
      await api(`/api/jobs/${id}`, { method: "DELETE" });
      if (id === jobId) {
        const j = await api<Job>(`/api/jobs/${id}`).catch(() => null);
        setJob(j);
      }
      void loadHistory();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Could not cancel the job");
    }
  }

  async function disconnectYouTube() {
    await fetch("/api/youtube/disconnect", { method: "POST" });
    void loadYouTube();
  }

  async function signOut() {
    await fetch("/api/login", { method: "DELETE" });
    window.location.href = "/login";
  }

  const connectYouTube = () => {
    window.location.href = "/api/youtube/auth";
  };

  const active = job && (job.status === "queued" || job.status === "running");

  return (
    <div className="mx-auto max-w-7xl px-4 pb-16 sm:px-6">
      {/* Header */}
      <header className="flex flex-wrap items-center justify-between gap-3 py-6">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-brand-cyan to-brand-pink shadow-glow">
            <Clapperboard className="h-5 w-5 text-ink-950" />
          </div>
          <div>
            <h1 className="font-display text-xl font-semibold tracking-tight">ClipSafe AI</h1>
            <p className="text-xs text-zinc-400">Long video in. Original Shorts out.</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {youtube.connected ? (
            <div className="flex items-center gap-2 rounded-xl border border-white/10 bg-white/[0.03] py-1.5 pl-3 pr-1.5 text-sm">
              <span className="h-2 w-2 rounded-full bg-emerald-400" />
              <span className="max-w-[10rem] truncate text-zinc-200">{youtube.channelTitle || "YouTube connected"}</span>
              <button
                type="button"
                onClick={disconnectYouTube}
                className="rounded-lg p-1.5 text-zinc-400 hover:bg-white/10 hover:text-zinc-100"
                aria-label="Disconnect YouTube"
                title="Disconnect YouTube"
              >
                <Unplug className="h-4 w-4" />
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="btn-ghost"
              onClick={connectYouTube}
              disabled={!youtube.configured}
              title={youtube.configured ? undefined : "Add a Google OAuth client in deploy settings to enable uploads"}
            >
              <Link2 className="h-4 w-4" />
              Connect YouTube
            </button>
          )}
          <button type="button" className="btn-ghost px-3" onClick={signOut} aria-label="Sign out" title="Sign out">
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </header>

      {banner && (
        <div className="mb-5 flex items-center justify-between gap-3 rounded-xl border border-brand-cyan/30 bg-brand-cyan/10 px-4 py-2.5 text-sm text-cyan-100">
          {banner}
          <button type="button" onClick={() => setBanner(null)} className="text-cyan-200/70 hover:text-cyan-100" aria-label="Dismiss">
            <XCircle className="h-4 w-4" />
          </button>
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="min-w-0 space-y-6">
          {/* Create form */}
          <form onSubmit={submit} className="card p-5 sm:p-6">
            <div className="mb-5 flex items-start justify-between gap-4">
              <div>
                <h2 className="font-display text-lg font-semibold">Create Shorts</h2>
                <p className="mt-1 text-sm text-zinc-400">
                  Gemini finds the best moments, then each Short gets your AI commentary, original B-roll, speaker
                  tracking and animated captions.
                </p>
              </div>
            </div>

            <label htmlFor="url" className="label">
              YouTube URL
            </label>
            <div className="flex flex-col gap-3 sm:flex-row">
              <div className="relative flex-1">
                <Film className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
                <input
                  id="url"
                  type="url"
                  inputMode="url"
                  required
                  placeholder="https://www.youtube.com/watch?v=…"
                  className="input pl-9"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                />
              </div>
              <button type="submit" className="btn-primary sm:w-44" disabled={submitting || !url || !rights}>
                {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                Generate
              </button>
            </div>

            <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div>
                <label htmlFor="clips" className="label">
                  Shorts: {maxClips}
                </label>
                <input
                  id="clips"
                  type="range"
                  min={1}
                  max={5}
                  value={maxClips}
                  onChange={(e) => setMaxClips(Number(e.target.value))}
                  className="mt-2 w-full"
                />
              </div>
              <div>
                <label htmlFor="length" className="label">
                  Clip length
                </label>
                <select id="length" className="input" value={length} onChange={(e) => setLength(e.target.value)}>
                  {LENGTHS.map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.label}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="lang" className="label">
                  Video language
                </label>
                <select id="lang" className="input" value={language} onChange={(e) => setLanguage(e.target.value)}>
                  {LANGUAGES.map((l) => (
                    <option key={l.code} value={l.code}>
                      {l.label}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="voice" className="label">
                  Commentary voice
                </label>
                <select id="voice" className="input" value={voice} onChange={(e) => setVoice(e.target.value)}>
                  {VOICES.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="mt-4 grid gap-4 sm:grid-cols-[minmax(0,1fr)_auto]">
              <div>
                <label htmlFor="style" className="label">
                  Commentary style
                </label>
                <input
                  id="style"
                  className="input"
                  maxLength={200}
                  value={style}
                  onChange={(e) => setStyle(e.target.value)}
                  placeholder="e.g. calm teacher, witty tech reviewer"
                />
              </div>
              <div>
                <span className="label">Caption highlight</span>
                <div className="flex h-[42px] items-center gap-2">
                  {CAPTION_COLORS.map((c) => (
                    <button
                      key={c}
                      type="button"
                      onClick={() => setCaptionColor(c)}
                      className={`h-7 w-7 rounded-full border-2 transition ${
                        captionColor === c ? "scale-110 border-white" : "border-transparent opacity-70 hover:opacity-100"
                      }`}
                      style={{ backgroundColor: c }}
                      aria-label={`Caption color ${c}`}
                    />
                  ))}
                </div>
              </div>
            </div>

            <label className="mt-5 flex cursor-pointer items-start gap-3 rounded-xl border border-white/[0.07] bg-ink-850/60 p-3.5 text-sm">
              <input
                type="checkbox"
                className="mt-0.5 h-4 w-4 shrink-0"
                checked={rights}
                onChange={(e) => setRights(e.target.checked)}
              />
              <span className="text-zinc-300">
                <span className="inline-flex items-center gap-1.5 font-medium text-zinc-100">
                  <ShieldCheck className="h-4 w-4 text-brand-lime" />I own this video or have permission to clip and
                  republish it.
                </span>
                <span className="mt-1 block text-xs text-zinc-400">
                  ClipSafe AI adds original commentary and visuals, but it does not replace the rights holder&apos;s
                  permission and is not a way around Content ID.
                </span>
              </span>
            </label>

            {formError && (
              <p className="mt-4 flex items-center gap-2 text-sm text-rose-400">
                <AlertTriangle className="h-4 w-4 shrink-0" />
                {formError}
              </p>
            )}
          </form>

          {/* Pipeline feature strip (shown before any job) */}
          {!job && !jobId && (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              {[
                { icon: Sparkles, t: "Gemini", d: "Viral moments" },
                { icon: Mic, t: "Vocal isolation", d: "demucs" },
                { icon: Wand2, t: "AI commentary", d: "Chirp 3 HD" },
                { icon: ImageIcon, t: "B-roll", d: "Imagen 3" },
                { icon: ScanFace, t: "Face tracking", d: "9:16 reframe" },
                { icon: Captions, t: "Captions", d: "Word-level" },
              ].map(({ icon: Icon, t, d }) => (
                <div key={t} className="card flex flex-col gap-2 p-4">
                  <Icon className="h-5 w-5 text-brand-cyan" />
                  <div className="text-sm font-medium">{t}</div>
                  <div className="text-xs text-zinc-500">{d}</div>
                </div>
              ))}
            </div>
          )}

          {/* Job status */}
          {jobId && !job && (
            <div className="card flex items-center gap-3 p-5 text-sm text-zinc-400">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading job…
            </div>
          )}

          {job && (
            <section className="card p-5 sm:p-6">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="truncate font-display text-lg font-semibold">
                    {job.source_title || (job.status === "failed" ? "Job failed" : "Working on your video")}
                  </h2>
                  <a
                    href={job.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="block truncate text-xs text-zinc-500 hover:text-zinc-300"
                  >
                    {job.source_channel ? `${job.source_channel} · ` : ""}
                    {job.url}
                  </a>
                </div>
                <div className="flex items-center gap-2">
                  {active && (
                    <button type="button" className="btn-ghost text-rose-300" onClick={() => cancelOrDelete(job.job_id)}>
                      <XCircle className="h-4 w-4" /> Cancel
                    </button>
                  )}
                  {job.status === "succeeded" && (
                    <button
                      type="button"
                      className="btn-ghost"
                      onClick={() => selectJob(job.job_id)}
                      title="Refresh signed links"
                    >
                      <RefreshCw className="h-4 w-4" />
                    </button>
                  )}
                </div>
              </div>

              {/* Stage tracker */}
              <ol className="mt-5 grid grid-cols-4 gap-2">
                {STAGES.map((s, i) => {
                  const current = stageIndex(job.stage, job.status);
                  const done = i < current || job.status === "succeeded";
                  const now = i === current && active;
                  const Icon = s.icon;
                  return (
                    <li key={s.key} className="flex flex-col items-center gap-1.5 text-center">
                      <div
                        className={`flex h-9 w-9 items-center justify-center rounded-full border transition ${
                          done
                            ? "border-transparent bg-gradient-to-br from-brand-cyan to-brand-pink text-ink-950"
                            : now
                              ? "border-brand-cyan/60 bg-brand-cyan/10 text-brand-cyan"
                              : "border-white/10 text-zinc-600"
                        }`}
                      >
                        {now ? <Loader2 className="h-4 w-4 animate-spin" /> : <Icon className="h-4 w-4" />}
                      </div>
                      <span className={`text-[11px] ${done || now ? "text-zinc-200" : "text-zinc-500"}`}>{s.label}</span>
                    </li>
                  );
                })}
              </ol>

              {job.status !== "succeeded" && (
                <div className="mt-5">
                  <div className="h-2 overflow-hidden rounded-full bg-white/[0.06]">
                    <div
                      className={`h-full rounded-full transition-all duration-700 ${
                        job.status === "failed" || job.status === "cancelled"
                          ? "bg-rose-500/70"
                          : "bg-[linear-gradient(90deg,#22D3EE,#F472B6,#22D3EE)] bg-[length:200%_100%] animate-shimmer"
                      }`}
                      style={{ width: `${Math.max(3, job.progress * 100)}%` }}
                    />
                  </div>
                  <p
                    className={`mt-2 flex items-center gap-2 text-sm ${
                      job.status === "failed" ? "text-rose-400" : "text-zinc-400"
                    }`}
                  >
                    {job.status === "failed" && <AlertTriangle className="h-4 w-4 shrink-0" />}
                    {job.error || job.message}
                    {active && <span className="text-zinc-600">· {Math.round(job.progress * 100)}%</span>}
                  </p>
                  {active && (
                    <p className="mt-1 text-xs text-zinc-500">
                      Rendering takes a few minutes per Short. You can close this tab and come back later.
                    </p>
                  )}
                </div>
              )}

              {job.video_summary && job.status === "succeeded" && (
                <p className="mt-5 rounded-xl border border-white/[0.06] bg-ink-850/60 p-3.5 text-sm text-zinc-300">
                  {job.video_summary}
                </p>
              )}
            </section>
          )}

          {/* Results */}
          {job?.status === "succeeded" && job.clips.length > 0 && (
            <section>
              <h3 className="mb-3 font-display text-base font-semibold text-zinc-200">
                {job.clips.length} Short{job.clips.length > 1 ? "s" : ""} ready
              </h3>
              <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
                {job.clips.map((clip) => (
                  <VideoPreviewCard
                    key={`${job.job_id}-${clip.index}`}
                    jobId={job.job_id}
                    clip={clip}
                    youtube={youtube}
                    onConnectYouTube={connectYouTube}
                  />
                ))}
              </div>
            </section>
          )}
        </div>

        {/* History */}
        <aside className="card h-fit p-4 lg:sticky lg:top-6">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-sm font-semibold text-zinc-200">
              <History className="h-4 w-4 text-zinc-400" /> Recent jobs
            </h2>
            <button
              type="button"
              onClick={() => void loadHistory()}
              className="rounded-lg p-1.5 text-zinc-500 hover:bg-white/10 hover:text-zinc-200"
              aria-label="Refresh"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </button>
          </div>
          {history.length === 0 ? (
            <p className="py-6 text-center text-xs text-zinc-500">No jobs in the last 24 hours.</p>
          ) : (
            <ul className="space-y-1.5">
              {history.map((h) => (
                <li key={h.job_id}>
                  <div
                    className={`group flex items-center gap-2 rounded-xl border px-3 py-2.5 transition ${
                      h.job_id === jobId
                        ? "border-brand-cyan/40 bg-brand-cyan/[0.06]"
                        : "border-transparent hover:border-white/10 hover:bg-white/[0.03]"
                    }`}
                  >
                    <button type="button" onClick={() => selectJob(h.job_id)} className="min-w-0 flex-1 text-left">
                      <div className="truncate text-sm text-zinc-200">{h.source_title || h.url}</div>
                      <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-zinc-500">
                        <StatusDot status={h.status} />
                        {h.status === "succeeded"
                          ? `${h.clip_count} Short${h.clip_count === 1 ? "" : "s"}`
                          : h.status === "running"
                            ? `${Math.round(h.progress * 100)}%`
                            : h.status}
                        <span>· {timeAgo(h.created_at)}</span>
                      </div>
                    </button>
                    <button
                      type="button"
                      onClick={() => cancelOrDelete(h.job_id)}
                      className="rounded-lg p-1.5 text-zinc-600 opacity-0 transition hover:bg-white/10 hover:text-rose-300 group-hover:opacity-100"
                      aria-label={h.status === "running" || h.status === "queued" ? "Cancel job" : "Delete media"}
                      title={h.status === "running" || h.status === "queued" ? "Cancel job" : "Delete media now"}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-4 border-t border-white/[0.06] pt-3 text-[11px] leading-relaxed text-zinc-500">
            Videos and job data are deleted automatically after 24 hours.
          </p>
        </aside>
      </div>
    </div>
  );
}

function StatusDot({ status }: { status: string }) {
  const color =
    status === "succeeded"
      ? "bg-emerald-400"
      : status === "failed"
        ? "bg-rose-500"
        : status === "cancelled"
          ? "bg-zinc-500"
          : "bg-brand-cyan animate-pulse";
  return <span className={`inline-block h-1.5 w-1.5 rounded-full ${color}`} />;
}
