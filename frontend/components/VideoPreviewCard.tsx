"use client";

import { useMemo, useRef, useState } from "react";
import {
  Check,
  CheckCircle2,
  ChevronDown,
  Clock,
  Copy,
  Download,
  ExternalLink,
  Flame,
  Loader2,
  Pause,
  Play,
  Upload,
  Volume2,
  VolumeX,
} from "lucide-react";
import type { Clip, PrivacyStatus, UploadResult, YouTubeStatus } from "@/lib/types";

interface Props {
  jobId: string;
  clip: Clip;
  youtube: YouTubeStatus;
  onConnectYouTube: () => void;
}

function formatTime(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function scoreTone(score: number): string {
  if (score >= 80) return "from-brand-pink to-orange-400 text-ink-950";
  if (score >= 60) return "from-brand-cyan to-brand-lime text-ink-950";
  return "from-zinc-500 to-zinc-400 text-ink-950";
}

export default function VideoPreviewCard({ jobId, clip, youtube, onConnectYouTube }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(true);
  const [progress, setProgress] = useState(0);

  const [title, setTitle] = useState(clip.title);
  const [description, setDescription] = useState(clip.description);
  const [privacy, setPrivacy] = useState<PrivacyStatus>("private");
  const [madeForKids, setMadeForKids] = useState(false);
  const [syntheticMedia, setSyntheticMedia] = useState(true);
  const [showDetails, setShowDetails] = useState(false);

  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploaded, setUploaded] = useState<UploadResult | null>(null);
  const [copied, setCopied] = useState(false);

  const expiresIn = useMemo(() => {
    const ms = new Date(clip.expires_at).getTime() - Date.now();
    if (!Number.isFinite(ms) || ms <= 0) return "expired";
    const h = Math.floor(ms / 3_600_000);
    const m = Math.floor((ms % 3_600_000) / 60_000);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  }, [clip.expires_at]);

  function togglePlay() {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) {
      void v.play();
    } else {
      v.pause();
    }
  }

  function toggleMute() {
    const v = videoRef.current;
    if (!v) return;
    v.muted = !v.muted;
    setMuted(v.muted);
  }

  async function copyDescription() {
    try {
      await navigator.clipboard.writeText(`${title}\n\n${description}`);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  }

  async function upload() {
    setUploading(true);
    setUploadError(null);
    try {
      const res = await fetch("/api/youtube/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jobId,
          index: clip.index,
          title,
          description,
          tags: clip.hashtags,
          privacyStatus: privacy,
          madeForKids,
          containsSyntheticMedia: syntheticMedia,
        }),
      });
      const data = (await res.json().catch(() => ({}))) as UploadResult & { error?: string };
      if (!res.ok) throw new Error(data.error || `Upload failed (${res.status})`);
      setUploaded(data);
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  return (
    <article className="card group flex flex-col overflow-hidden transition hover:border-white/15">
      {/* 9:16 player */}
      <div className="relative aspect-[9/16] w-full overflow-hidden bg-black">
        <video
          ref={videoRef}
          src={clip.video_url}
          poster={clip.thumbnail_url}
          className="h-full w-full object-cover"
          playsInline
          muted={muted}
          loop
          preload="metadata"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onTimeUpdate={(e) => {
            const v = e.currentTarget;
            setProgress(v.duration ? v.currentTime / v.duration : 0);
          }}
          onClick={togglePlay}
        />
        <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between p-3">
          <span
            className={`inline-flex items-center gap-1 rounded-full bg-gradient-to-r px-2.5 py-1 text-xs font-bold ${scoreTone(
              clip.viral_score,
            )}`}
          >
            <Flame className="h-3.5 w-3.5" />
            {clip.viral_score}
          </span>
          <span className="chip bg-black/50 backdrop-blur">
            <Clock className="h-3.5 w-3.5" />
            {formatTime(clip.duration_seconds)}
          </span>
        </div>
        <div className="absolute inset-x-0 bottom-0 flex items-center gap-2 bg-gradient-to-t from-black/80 to-transparent p-3 pt-10">
          <button
            type="button"
            onClick={togglePlay}
            className="flex h-9 w-9 items-center justify-center rounded-full bg-white/90 text-ink-950 transition hover:bg-white"
            aria-label={playing ? "Pause" : "Play"}
          >
            {playing ? <Pause className="h-4 w-4" /> : <Play className="ml-0.5 h-4 w-4" />}
          </button>
          <div className="h-1 flex-1 overflow-hidden rounded-full bg-white/20">
            <div className="h-full bg-gradient-to-r from-brand-cyan to-brand-pink" style={{ width: `${progress * 100}%` }} />
          </div>
          <button
            type="button"
            onClick={toggleMute}
            className="flex h-9 w-9 items-center justify-center rounded-full bg-black/50 text-white backdrop-blur transition hover:bg-black/70"
            aria-label={muted ? "Unmute" : "Mute"}
          >
            {muted ? <VolumeX className="h-4 w-4" /> : <Volume2 className="h-4 w-4" />}
          </button>
        </div>
      </div>

      {/* Details */}
      <div className="flex flex-1 flex-col gap-3 p-4">
        <div>
          <label className="label" htmlFor={`title-${clip.index}`}>
            Title
          </label>
          <input
            id={`title-${clip.index}`}
            className="input font-medium"
            value={title}
            maxLength={100}
            onChange={(e) => setTitle(e.target.value)}
          />
        </div>

        <p className="text-xs leading-relaxed text-zinc-400">
          <span className="text-zinc-300">Why it works:</span> {clip.reason}
        </p>

        <div className="flex flex-wrap gap-1.5">
          {clip.hashtags.map((h) => (
            <span key={h} className="chip">
              #{h}
            </span>
          ))}
          <span className="chip text-zinc-500">
            {formatTime(clip.start_seconds)}–{formatTime(clip.end_seconds)} in source
          </span>
        </div>

        <button
          type="button"
          onClick={() => setShowDetails((s) => !s)}
          className="flex items-center gap-1 self-start text-xs font-medium text-zinc-400 hover:text-zinc-200"
        >
          <ChevronDown className={`h-4 w-4 transition ${showDetails ? "rotate-180" : ""}`} />
          {showDetails ? "Hide" : "Edit"} description and upload settings
        </button>

        {showDetails && (
          <div className="space-y-3 rounded-xl border border-white/[0.06] bg-ink-850/60 p-3">
            <div>
              <label className="label" htmlFor={`desc-${clip.index}`}>
                Description
              </label>
              <textarea
                id={`desc-${clip.index}`}
                className="input min-h-[120px] resize-y text-xs leading-relaxed"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <div>
                <label className="label" htmlFor={`privacy-${clip.index}`}>
                  Visibility
                </label>
                <select
                  id={`privacy-${clip.index}`}
                  className="input"
                  value={privacy}
                  onChange={(e) => setPrivacy(e.target.value as PrivacyStatus)}
                >
                  <option value="private">Private</option>
                  <option value="unlisted">Unlisted</option>
                  <option value="public">Public</option>
                </select>
              </div>
              <div className="flex flex-col justify-end gap-2 pb-1 text-xs text-zinc-300">
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={madeForKids} onChange={(e) => setMadeForKids(e.target.checked)} />
                  Made for kids
                </label>
                <label className="flex items-center gap-2" title="YouTube asks creators to disclose realistic AI-generated or altered media">
                  <input
                    type="checkbox"
                    checked={syntheticMedia}
                    onChange={(e) => setSyntheticMedia(e.target.checked)}
                  />
                  Disclose AI voice / imagery
                </label>
              </div>
            </div>
            {clip.transcript && (
              <details className="text-xs text-zinc-400">
                <summary className="cursor-pointer text-zinc-300">Transcript</summary>
                <p className="mt-2 leading-relaxed">{clip.transcript}</p>
              </details>
            )}
          </div>
        )}

        <div className="mt-auto space-y-2 pt-1">
          {uploaded ? (
            <a
              href={uploaded.url}
              target="_blank"
              rel="noopener noreferrer"
              className="btn w-full border border-emerald-400/30 bg-emerald-400/10 text-emerald-300 hover:bg-emerald-400/15"
            >
              <CheckCircle2 className="h-4 w-4" />
              Uploaded. Open on YouTube
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          ) : youtube.connected ? (
            <button type="button" className="btn-primary w-full" onClick={upload} disabled={uploading || expiresIn === "expired"}>
              {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
              {uploading ? "Uploading to YouTube…" : `Upload as ${privacy}`}
            </button>
          ) : (
            <button
              type="button"
              className="btn-ghost w-full"
              onClick={onConnectYouTube}
              disabled={!youtube.configured}
              title={youtube.configured ? undefined : "Add a Google OAuth client to enable uploads"}
            >
              <Upload className="h-4 w-4" />
              {youtube.configured ? "Connect YouTube to upload" : "YouTube upload not configured"}
            </button>
          )}
          {uploadError && <p className="text-xs text-rose-400">{uploadError}</p>}

          <div className="grid grid-cols-2 gap-2">
            <a href={clip.download_url} className="btn-ghost" download>
              <Download className="h-4 w-4" />
              Download
            </a>
            <button type="button" className="btn-ghost" onClick={copyDescription}>
              {copied ? <Check className="h-4 w-4 text-emerald-400" /> : <Copy className="h-4 w-4" />}
              {copied ? "Copied" : "Copy text"}
            </button>
          </div>
          <p className="text-center text-[11px] text-zinc-500">Link expires in {expiresIn}. Media auto-deletes after 24h.</p>
        </div>
      </div>
    </article>
  );
}
