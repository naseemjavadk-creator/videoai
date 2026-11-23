"""
ClipSafe AI media pipeline.

Turns a long-form YouTube video (one the user owns or has the rights to clip) into
vertical 9:16 Shorts with original, transformative layers:

    yt-dlp ............. download the source and a low-res analysis proxy
    Gemini 2.5 Pro ..... pick the strongest segments, write commentary + metadata (JSON)
    demucs ............. isolate the spoken vocals of each segment
    Cloud TTS .......... Chirp 3 HD voiceover for the intro and outro commentary
    Imagen 3 ........... original B-roll images for the split screen and title cards
    Video Intelligence . face tracks to keep the speaker framed in the crop
    Speech-to-Text V2 .. word-level timestamps for animated captions
    FFmpeg ............. one multi-layer filtergraph renders the final Short

Every blocking SDK call runs in a worker thread and every external binary runs as an
asyncio subprocess, so the FastAPI event loop stays responsive while jobs run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("clipsafe.pipeline")

# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (set by deploy.sh)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "gcp": Google Cloud services (Vertex AI, Cloud TTS, Speech, Video Intelligence, GCS)
    # "local": free stack on your own computer (AI Studio key, edge-tts, Whisper, OpenCV,
    #          Pexels, local folder). See local_providers.py.
    mode: str = "gcp"
    gcp_project: str = ""
    gcs_bucket: str = ""
    vertex_location: str = "us-central1"
    speech_location: str = "us-central1"
    speech_model: str = "chirp_2"
    gemini_model: str = "gemini-2.5-pro"
    imagen_model: str = "imagen-3.0-generate-001"
    tts_voice: str = "en-US-Chirp3-HD-Charon"
    service_account_email: Optional[str] = None
    signed_url_ttl_seconds: int = 21600
    max_source_duration_seconds: int = 3600
    allowed_origins: str = "*"
    api_key: Optional[str] = None
    ytdlp_cookies_path: Optional[str] = None
    work_dir: str = "/tmp/clipsafe"
    demucs_model: str = "htdemucs"
    max_concurrent_jobs: int = 1
    max_clips_per_job: int = 5

    # Local mode
    gemini_api_key: Optional[str] = None
    gemini_fallback_model: str = "gemini-2.5-flash"
    pexels_api_key: Optional[str] = None
    whisper_model: str = "small"
    yunet_model_path: str = "models/face_detection_yunet_2023mar.onnx"
    data_dir: str = "../data"
    public_base_url: str = "http://127.0.0.1:8080"
    retention_hours: int = 24

    @property
    def is_local(self) -> bool:
        return self.mode.lower() == "local"

    # Render constants
    out_width: int = 1080
    out_height: int = 1920
    out_fps: int = 30


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    if s.is_local:
        if not s.gemini_api_key:
            raise RuntimeError("MODE=local needs GEMINI_API_KEY (free key from https://aistudio.google.com/apikey)")
    elif not (s.gcp_project and s.gcs_bucket):
        raise RuntimeError("MODE=gcp needs GCP_PROJECT and GCS_BUCKET")
    return s


def local_path(key: str) -> Path:
    """Map a storage key like 'jobs/abc/clips/00/short.mp4' to a file in DATA_DIR (local mode)."""
    root = Path(get_settings().data_dir).expanduser().resolve()
    target = (root / key).resolve()
    if root not in target.parents and target != root:
        raise PipelineError("Invalid storage path")
    return target


# --------------------------------------------------------------------------------------
# Errors and shared types
# --------------------------------------------------------------------------------------


class PipelineError(RuntimeError):
    """A failure the user should see, with a readable message."""


ProgressCallback = Callable[[str, float, str], Awaitable[None]]


async def _noop_progress(stage: str, pct: float, message: str) -> None:  # pragma: no cover
    return None


class JobOptions(BaseModel):
    """What the user asked for. Validated again in main.py at the API boundary."""

    url: str
    max_clips: int = Field(default=3, ge=1, le=5)
    min_clip_seconds: int = Field(default=20, ge=10, le=90)
    max_clip_seconds: int = Field(default=50, ge=15, le=120)
    language_code: str = Field(default="en-US", min_length=2, max_length=16)
    tts_voice: Optional[str] = None
    commentary_style: str = Field(default="energetic, insightful creator", max_length=200)
    caption_color: str = Field(default="#FFE14D", pattern=r"^#[0-9A-Fa-f]{6}$")
    rights_confirmed: bool = False

    @field_validator("max_clip_seconds")
    @classmethod
    def _max_after_min(cls, v: int, info: Any) -> int:
        lo = info.data.get("min_clip_seconds", 10)
        if v <= lo:
            raise ValueError("max_clip_seconds must be greater than min_clip_seconds")
        return v


# ---- Gemini structured output schema -------------------------------------------------


class ViralSegment(BaseModel):
    start_seconds: float = Field(description="Segment start in seconds from the start of the video.")
    end_seconds: float = Field(description="Segment end in seconds from the start of the video.")
    title: str = Field(description="YouTube Shorts title, under 70 characters, no clickbait lies.")
    hook_text: str = Field(description="On-screen hook for the title card, 3 to 7 words.")
    intro_commentary: str = Field(
        description="Original spoken commentary (15-25 words) that frames why this moment matters."
    )
    outro_commentary: str = Field(
        description="Original spoken takeaway or question to the viewer (10-20 words)."
    )
    broll_search_terms: list[str] = Field(
        default_factory=list,
        description="Exactly 3 short stock-photo search terms (1-3 English words each) for the B-roll.",
    )
    broll_prompts: list[str] = Field(
        description="Exactly 3 vivid image prompts for original B-roll that illustrate the topic. "
        "No real people, no logos, no copyrighted characters, no text."
    )
    description: str = Field(description="YouTube description, 1-3 sentences.")
    hashtags: list[str] = Field(description="3 to 6 hashtags without the # sign.")
    viral_score: int = Field(description="Predicted engagement from 1 to 100.")
    reason: str = Field(description="One sentence on why this segment works as a Short.")


class ViralPlan(BaseModel):
    video_summary: str
    segments: list[ViralSegment]


# ---- Intermediate artefacts ------------------------------------------------------------


@dataclass
class SourceVideo:
    path: Path
    proxy_path: Path
    title: str
    channel: str
    duration: float
    width: int
    height: int
    webpage_url: str


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class CropPlan:
    width: int
    height: int
    y: int
    keyframes: list[tuple[float, int]]  # (time_seconds, x)


@dataclass
class ClipResult:
    index: int
    title: str
    description: str
    hashtags: list[str]
    viral_score: int
    reason: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    video_gcs_path: str
    thumbnail_gcs_path: str
    captions_gcs_path: str
    transcript: str


@dataclass
class PipelineResult:
    job_id: str
    source_title: str
    source_channel: str
    source_url: str
    video_summary: str
    clips: list[ClipResult] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Google Cloud clients (lazy singletons, thread-safe for concurrent use)
# --------------------------------------------------------------------------------------


# Google Cloud libraries are imported inside these factories so local mode does not need them.


@lru_cache(maxsize=1)
def genai_client() -> genai.Client:
    s = get_settings()
    if s.is_local:
        return genai.Client(api_key=s.gemini_api_key)
    return genai.Client(vertexai=True, project=s.gcp_project, location=s.vertex_location)


@lru_cache(maxsize=1)
def storage_client() -> Any:
    from google.cloud import storage

    return storage.Client(project=get_settings().gcp_project)


@lru_cache(maxsize=1)
def tts_client() -> Any:
    from google.cloud import texttospeech

    return texttospeech.TextToSpeechClient()


@lru_cache(maxsize=1)
def video_client() -> Any:
    from google.cloud import videointelligence

    return videointelligence.VideoIntelligenceServiceClient()


@lru_cache(maxsize=1)
def speech_client() -> Any:
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient

    loc = get_settings().speech_location
    endpoint = "speech.googleapis.com" if loc == "global" else f"{loc}-speech.googleapis.com"
    return SpeechClient(client_options=ClientOptions(api_endpoint=endpoint))


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


async def run_cmd(
    args: list[str],
    cwd: Optional[Path] = None,
    timeout: float = 1800,
    env: Optional[dict[str, str]] = None,
) -> str:
    """Run an external command; raise PipelineError with the tail of stderr on failure."""
    logger.debug("exec: %s", " ".join(args))
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise PipelineError(f"{Path(args[0]).name} timed out after {int(timeout)}s")
    if proc.returncode != 0:
        tail = stderr.decode("utf-8", "replace").strip().splitlines()[-25:]
        logger.error("command failed (%s): %s\n%s", proc.returncode, " ".join(args), "\n".join(tail))
        raise PipelineError(f"{Path(args[0]).name} failed: {tail[-1] if tail else 'unknown error'}")
    return stdout.decode("utf-8", "replace")


async def ffprobe_duration(path: Path) -> float:
    out = await run_cmd(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        timeout=120,
    )
    try:
        return float(out.strip())
    except ValueError as exc:
        raise PipelineError(f"Could not read duration of {path.name}") from exc


async def ffprobe_video_size(path: Path) -> tuple[int, int]:
    out = await run_cmd(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", str(path),
        ],
        timeout=120,
    )
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    w, h = int(stream.get("width", 0)), int(stream.get("height", 0))
    if w <= 0 or h <= 0:
        raise PipelineError(f"{path.name} has no readable video stream")
    return w, h


def _even(n: float) -> int:
    v = int(round(n))
    return v - (v % 2)


def _td_seconds(value: Any) -> float:
    """Proto Duration / timedelta / None -> float seconds."""
    if value is None:
        return 0.0
    if isinstance(value, timedelta):
        return value.total_seconds()
    seconds = getattr(value, "seconds", 0) or 0
    nanos = getattr(value, "nanos", 0) or getattr(value, "microseconds", 0) * 1000
    return float(seconds) + float(nanos) / 1e9


@lru_cache(maxsize=4)
def font_file(pattern: str = "Noto Sans:bold") -> str:
    """Resolve a font file path through fontconfig (installed in the Docker image)."""
    try:
        path = subprocess.run(
            ["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        if path and Path(path).exists():
            return path
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    for candidate in (
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if Path(candidate).exists():
            return candidate
    raise PipelineError("No bold font found on this system")


def font_for_language(language_code: str) -> str:
    """Bold font that covers the script of the video language (Malayalam, Devanagari, …)."""
    lang = (language_code or "en").split("-")[0].lower()
    return font_file(f"Noto Sans:bold:lang={lang}")


def _font_family(path: str) -> str:
    try:
        return subprocess.run(
            ["fc-query", "-f", "%{family[0]}", path], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip() or "Noto Sans"
    except (subprocess.SubprocessError, FileNotFoundError):
        return "Noto Sans"


def _wrap(text: str, width: int, max_lines: int) -> str:
    lines = textwrap.wrap(" ".join(text.split()), width=width, break_long_words=True)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,;:") + "…"
    return "\n".join(lines)


def _sanitize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise PipelineError("Please paste a full YouTube URL starting with https://")
    if not re.search(r"(youtube\.com|youtu\.be)/", url):
        raise PipelineError("Only YouTube URLs are supported")
    return url


# --------------------------------------------------------------------------------------
# Cloud Storage
# --------------------------------------------------------------------------------------


def _upload_sync(local: Path, gcs_path: str, content_type: str) -> str:
    if get_settings().is_local:
        target = local_path(gcs_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, target)
        return f"local://{gcs_path}"
    bucket = storage_client().bucket(get_settings().gcs_bucket)
    blob = bucket.blob(gcs_path)
    blob.chunk_size = 16 * 1024 * 1024
    blob.upload_from_filename(str(local), content_type=content_type, timeout=900)
    return f"gs://{bucket.name}/{gcs_path}"


async def upload_file(local: Path, gcs_path: str, content_type: str) -> str:
    return await asyncio.to_thread(_upload_sync, local, gcs_path, content_type)


async def upload_json(data: Any, gcs_path: str) -> str:
    def _do() -> str:
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if get_settings().is_local:
            target = local_path(gcs_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            return f"local://{gcs_path}"
        bucket = storage_client().bucket(get_settings().gcs_bucket)
        blob = bucket.blob(gcs_path)
        blob.upload_from_string(text, content_type="application/json")
        return f"gs://{bucket.name}/{gcs_path}"

    return await asyncio.to_thread(_do)


# --------------------------------------------------------------------------------------
# Step 1: yt-dlp download
# --------------------------------------------------------------------------------------


def _ytdlp_base_opts() -> dict[str, Any]:
    s = get_settings()
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        # Deno is baked into the image; yt-dlp-ejs ships the solver scripts locally.
        "js_runtimes": {"deno": {}},
    }
    if s.ytdlp_cookies_path and Path(s.ytdlp_cookies_path).exists():
        # Secret Manager mounts are read-only; yt-dlp rewrites its cookie jar, so copy it.
        writable = Path(s.work_dir) / "cookies.txt"
        writable.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(s.ytdlp_cookies_path, writable)
        opts["cookiefile"] = str(writable)
    return opts


def _download_sync(url: str, workdir: Path) -> dict[str, Any]:
    import yt_dlp  # imported lazily: heavy module

    s = get_settings()
    base = _ytdlp_base_opts()

    with yt_dlp.YoutubeDL({**base, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise PipelineError("Could not read that video")
    if info.get("_type") == "playlist":
        raise PipelineError("Playlists are not supported; paste a single video URL")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise PipelineError("Live streams are not supported; wait until the stream has ended")
    duration = float(info.get("duration") or 0)
    if duration <= 0:
        raise PipelineError("That video has no duration information")
    if duration > s.max_source_duration_seconds:
        raise PipelineError(
            f"Video is {int(duration // 60)} min long; the limit is {s.max_source_duration_seconds // 60} min"
        )

    dl_opts = {
        **base,
        "format": "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
    }
    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.download([info.get("webpage_url") or url])
    return info


async def download_source(url: str, workdir: Path, progress: ProgressCallback) -> SourceVideo:
    await progress("download", 0.02, "Downloading the source video")
    try:
        info = await asyncio.to_thread(_download_sync, url, workdir)
    except PipelineError:
        raise
    except Exception as exc:  # yt-dlp raises DownloadError and friends
        msg = str(exc)
        if "Sign in to confirm" in msg or "bot" in msg.lower():
            raise PipelineError(
                "YouTube asked for a sign-in check. Add cookies from your own account (see README) and retry."
            ) from exc
        if "Private video" in msg or "members-only" in msg.lower():
            raise PipelineError("That video is private or members-only") from exc
        raise PipelineError(f"Download failed: {msg.splitlines()[-1][:300]}") from exc

    path = workdir / "source.mp4"
    if not path.exists():
        candidates = sorted(workdir.glob("source.*"))
        if not candidates:
            raise PipelineError("Download finished but no video file was produced")
        path = candidates[0]

    width, height = await ffprobe_video_size(path)
    duration = await ffprobe_duration(path)

    # Low-res proxy keeps the Gemini upload small and fast (Gemini samples ~1 fps anyway).
    await progress("download", 0.08, "Preparing an analysis copy")
    proxy = workdir / "proxy.mp4"
    await run_cmd(
        [
            "ffmpeg", "-hide_banner", "-y", "-i", str(path),
            "-vf", "scale=-2:360,fps=5", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
            "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", str(proxy),
        ],
        timeout=1800,
    )

    return SourceVideo(
        path=path,
        proxy_path=proxy,
        title=str(info.get("title") or "Untitled"),
        channel=str(info.get("channel") or info.get("uploader") or "Unknown channel"),
        duration=duration,
        width=width,
        height=height,
        webpage_url=str(info.get("webpage_url") or url),
    )


# --------------------------------------------------------------------------------------
# Step 2: Gemini 2.5 Pro viral-moment extraction
# --------------------------------------------------------------------------------------

GEMINI_SYSTEM = """You are a senior short-form video editor and an original commentator.
You watch long videos and choose self-contained moments that work as YouTube Shorts.
For each moment you also write ORIGINAL commentary that adds context, analysis or a
takeaway, so the Short is a genuinely new work rather than a re-upload.
Rules:
- Segments must not overlap and must start and end on complete sentences.
- Prefer moments with a strong hook in the first 2 seconds, a clear payoff, and one speaker.
- Commentary is in the first person of a creator reacting to the clip; never claim to be
  the person in the video and never invent facts that are not in the video.
- B-roll prompts describe illustrative scenes, objects, places, diagrams or abstract
  visuals. Never describe real or identifiable people, logos, brands, or copyrighted
  characters, and never ask for text in the image.
- Write titles, commentary and descriptions in the same language as the video.
Return only JSON that matches the schema."""


async def extract_viral_plan(
    source: SourceVideo, opts: JobOptions, job_id: str, progress: ProgressCallback
) -> ViralPlan:
    await progress("analyze", 0.12, "Gemini is watching the video")
    s = get_settings()
    if s.is_local:
        from local_providers import gemini_upload_video

        try:
            proxy_uri, proxy_mime = await gemini_upload_video(genai_client(), source.proxy_path)
        except Exception as exc:
            raise PipelineError(f"Could not send the video to Gemini: {exc}") from exc
    else:
        proxy_uri = await upload_file(source.proxy_path, f"jobs/{job_id}/proxy.mp4", "video/mp4")
        proxy_mime = "video/mp4"

    prompt = f"""Video title: {source.title}
Channel: {source.channel}
Duration: {source.duration:.1f} seconds

Pick the {opts.max_clips} best moments for Shorts.
Each segment must be between {opts.min_clip_seconds} and {opts.max_clip_seconds} seconds long,
inside 0 and {source.duration:.1f} seconds.
Commentary voice and tone: {opts.commentary_style}.
Order segments by viral_score, highest first."""

    config = genai_types.GenerateContentConfig(
        system_instruction=GEMINI_SYSTEM,
        response_mime_type="application/json",
        response_schema=ViralPlan,
        temperature=0.5,
        max_output_tokens=16384,
        media_resolution=genai_types.MediaResolution.MEDIA_RESOLUTION_LOW,
    )
    contents = [
        genai_types.Content(
            role="user",
            parts=[
                genai_types.Part.from_uri(file_uri=proxy_uri, mime_type=proxy_mime),
                genai_types.Part.from_text(text=prompt),
            ],
        )
    ]

    response = None
    models = [s.gemini_model]
    if s.is_local and s.gemini_fallback_model and s.gemini_fallback_model != s.gemini_model:
        models.append(s.gemini_fallback_model)  # free-tier Pro quota is small; Flash has more
    last_exc: Optional[Exception] = None
    for model_name in models:
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=2, min=4, max=30),
                retry=retry_if_exception_type(Exception),
                reraise=True,
            ):
                with attempt:
                    response = await genai_client().aio.models.generate_content(
                        model=model_name, contents=contents, config=config
                    )
                    if response.parsed is None and not (response.text or "").strip():
                        raise PipelineError("Gemini returned an empty response")
            break
        except Exception as exc:
            last_exc = exc
            logger.warning("Gemini model %s failed: %s", model_name, exc)
            response = None
    if response is None:
        msg = str(last_exc or "")
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            raise PipelineError("Gemini free quota is used up for now. Try again later or tomorrow.") from last_exc
        raise PipelineError(f"Gemini request failed: {msg[:300]}") from last_exc

    assert response is not None
    plan: Optional[ViralPlan] = response.parsed if isinstance(response.parsed, ViralPlan) else None
    if plan is None:
        try:
            plan = ViralPlan.model_validate_json(response.text or "")
        except Exception as exc:
            raise PipelineError("Gemini did not return a valid clip plan") from exc

    plan.segments = _clean_segments(plan.segments, source.duration, opts)
    if not plan.segments:
        raise PipelineError("Gemini could not find a usable segment in this video")
    await progress("analyze", 0.2, f"Found {len(plan.segments)} moment(s)")
    return plan


def _clean_segments(segments: list[ViralSegment], duration: float, opts: JobOptions) -> list[ViralSegment]:
    """Clamp to the video, enforce length limits, drop overlaps, keep the best N."""
    cleaned: list[ViralSegment] = []
    for seg in sorted(segments, key=lambda x: x.viral_score, reverse=True):
        start = max(0.0, min(float(seg.start_seconds), duration - 1))
        end = max(start + 1, min(float(seg.end_seconds), duration))
        length = end - start
        if length < opts.min_clip_seconds:
            end = min(duration, start + opts.min_clip_seconds)
            start = max(0.0, end - opts.min_clip_seconds)
        if end - start > opts.max_clip_seconds:
            end = start + opts.max_clip_seconds
        if end - start < 5:
            continue
        if any(start < c.end_seconds and end > c.start_seconds for c in cleaned):
            continue
        seg.start_seconds, seg.end_seconds = round(start, 3), round(end, 3)
        prompts = [p.strip() for p in seg.broll_prompts if p.strip()]
        while len(prompts) < 3:
            prompts.append(f"Cinematic, abstract visual metaphor for: {seg.title}")
        seg.broll_prompts = prompts[:3]
        terms = [t.strip() for t in seg.broll_search_terms if t.strip()]
        while len(terms) < 3:
            terms.append(" ".join(seg.title.split()[:3]) or "abstract background")
        seg.broll_search_terms = terms[:3]
        seg.hashtags = [re.sub(r"[^\w]", "", h.lstrip("#")) for h in seg.hashtags if h.strip()][:6]
        seg.viral_score = max(1, min(100, int(seg.viral_score)))
        cleaned.append(seg)
        if len(cleaned) >= opts.max_clips:
            break
    return sorted(cleaned, key=lambda x: x.start_seconds)


# --------------------------------------------------------------------------------------
# Step 3: cut segment + demucs vocal isolation
# --------------------------------------------------------------------------------------

_DEMUCS_LOCK = asyncio.Semaphore(1)  # demucs saturates every CPU core; run one at a time


async def cut_segment(source: SourceVideo, seg: ViralSegment, out: Path) -> None:
    await run_cmd(
        [
            "ffmpeg", "-hide_banner", "-y",
            "-ss", f"{seg.start_seconds:.3f}", "-to", f"{seg.end_seconds:.3f}",
            "-i", str(source.path),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out),
        ],
        timeout=900,
    )


async def isolate_vocals(segment_video: Path, workdir: Path) -> Path:
    """Separate spoken vocals from background music/effects with demucs (htdemucs)."""
    s = get_settings()
    mix = workdir / "mix.wav"
    await run_cmd(
        ["ffmpeg", "-hide_banner", "-y", "-i", str(segment_video), "-vn", "-ac", "2", "-ar", "44100", str(mix)],
        timeout=300,
    )
    out_dir = workdir / "demucs"
    threads = str(max(1, os.cpu_count() or 1))
    async with _DEMUCS_LOCK:
        await run_cmd(
            [
                sys.executable, "-m", "demucs.separate",
                "-n", s.demucs_model, "--two-stems", "vocals", "-d", "cpu",
                "--filename", "{stem}.{ext}", "-o", str(out_dir), "-j", "1", str(mix),
            ],
            timeout=1800,
            env={"OMP_NUM_THREADS": threads, "MKL_NUM_THREADS": threads},
        )
    found = sorted(out_dir.rglob("vocals.wav"))
    if not found:
        raise PipelineError("Vocal isolation produced no output")
    vocals = workdir / "vocals.wav"
    shutil.move(str(found[0]), vocals)
    shutil.rmtree(out_dir, ignore_errors=True)
    mix.unlink(missing_ok=True)
    return vocals


# --------------------------------------------------------------------------------------
# Step 4: Cloud Text-to-Speech commentary
# --------------------------------------------------------------------------------------


def _tts_sync(text: str, voice_name: str, out: Path) -> None:
    from google.cloud import texttospeech

    lang = "-".join(voice_name.split("-")[:2]) if voice_name.count("-") >= 2 else "en-US"
    response = tts_client().synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(language_code=lang, name=voice_name),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=48000,
        ),
        timeout=120,
    )
    out.write_bytes(response.audio_content)


async def synthesize_voiceover(text: str, voice: str, out: Path, language_code: str = "en-US") -> float:
    text = " ".join(text.split())
    if not text:
        raise PipelineError("Commentary text is empty")
    if get_settings().is_local:
        from local_providers import edge_tts_to_wav, edge_voice_for

        try:
            await edge_tts_to_wav(text, edge_voice_for(voice, language_code), out, run_cmd)
        except Exception as exc:
            raise PipelineError(f"Voiceover failed: {exc}") from exc
        return await ffprobe_duration(out)
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15), reraise=True
    ):
        with attempt:
            await asyncio.to_thread(_tts_sync, text, voice, out)
    return await ffprobe_duration(out)


# --------------------------------------------------------------------------------------
# Step 5: Imagen 3 B-roll
# --------------------------------------------------------------------------------------

BROLL_STYLE = (
    "cinematic photograph, dramatic lighting, rich color, shallow depth of field, "
    "high detail, no text, no watermark, no logos, no people"
)


async def generate_broll(
    prompt: str,
    aspect_ratio: str,
    out: Path,
    fallback_color: str,
    search_term: str = "",
    fallback_video: Optional[Path] = None,
    fallback_at: float = 1.0,
) -> Path:
    """Get a B-roll still.

    gcp: generate an original image with Imagen 3. local: a free Pexels stock photo.
    Fallbacks: a blurred, colour-graded frame from the clip, then a plain vignette.
    """
    s = get_settings()
    if s.is_local:
        from local_providers import pexels_photo

        orientation = "portrait" if aspect_ratio == "9:16" else "square"
        try:
            if await pexels_photo(s.pexels_api_key or "", search_term or prompt, orientation, out):
                return out
        except Exception as exc:
            logger.warning("Pexels failed for %r: %s", search_term, exc)
        return await _fallback_still(out, aspect_ratio, fallback_color, fallback_video, fallback_at)
    config = genai_types.GenerateImagesConfig(
        number_of_images=1,
        aspect_ratio=aspect_ratio,
        output_mime_type="image/png",
        person_generation=genai_types.PersonGeneration.DONT_ALLOW,
        safety_filter_level=genai_types.SafetyFilterLevel.BLOCK_MEDIUM_AND_ABOVE,
        include_rai_reason=True,
    )
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20), reraise=True
        ):
            with attempt:
                result = await genai_client().aio.models.generate_images(
                    model=s.imagen_model, prompt=f"{prompt}. {BROLL_STYLE}", config=config
                )
                images = [g for g in (result.generated_images or []) if g.image and g.image.image_bytes]
                if not images:
                    reasons = [g.rai_filtered_reason for g in (result.generated_images or []) if g.rai_filtered_reason]
                    raise PipelineError(f"Imagen returned no image ({'; '.join(reasons) or 'filtered'})")
                out.write_bytes(images[0].image.image_bytes)  # type: ignore[union-attr]
                return out
    except Exception as exc:
        logger.warning("B-roll generation failed for %r, using fallback: %s", prompt[:80], exc)
    return await _fallback_still(out, aspect_ratio, fallback_color, fallback_video, fallback_at)


async def _fallback_still(
    out: Path, aspect_ratio: str, fallback_color: str, video: Optional[Path], at: float
) -> Path:
    size = "1080x1920" if aspect_ratio == "9:16" else "1080x1080"
    w, h = size.split("x")
    if video is not None and video.exists():
        try:
            await run_cmd(
                [
                    "ffmpeg", "-hide_banner", "-y", "-ss", f"{max(0.0, at):.2f}", "-i", str(video),
                    "-frames:v", "1",
                    "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                    f"gblur=sigma=28,eq=saturation=1.35:brightness=-0.06,vignette=angle=PI/4",
                    str(out),
                ],
                timeout=60,
            )
            return out
        except PipelineError:
            pass
    await run_cmd(
        [
            "ffmpeg", "-hide_banner", "-y", "-f", "lavfi",
            "-i", f"color=c=0x{fallback_color.lstrip('#')}:s={size}",
            "-vf", "vignette=angle=PI/3,noise=alls=12:allf=t", "-frames:v", "1", str(out),
        ],
        timeout=60,
    )
    return out


# --------------------------------------------------------------------------------------
# Step 6: Video Intelligence face tracking -> dynamic crop plan
# --------------------------------------------------------------------------------------


def _faces_sync(gcs_uri: str) -> list[tuple[float, float, float]]:
    """Return (time, center_x_normalized, area) samples for every detected face box."""
    from google.cloud import videointelligence as vi

    operation = video_client().annotate_video(
        request={
            "input_uri": gcs_uri,
            "features": [vi.Feature.FACE_DETECTION],
            "video_context": vi.VideoContext(
                face_detection_config=vi.FaceDetectionConfig(include_bounding_boxes=True)
            ),
        }
    )
    result = operation.result(timeout=1200)
    samples: list[tuple[float, float, float]] = []
    for annotation_result in result.annotation_results:
        for face in annotation_result.face_detection_annotations:
            for track in face.tracks:
                for obj in track.timestamped_objects:
                    box = obj.normalized_bounding_box
                    w = max(0.0, box.right - box.left)
                    h = max(0.0, box.bottom - box.top)
                    if w <= 0 or h <= 0:
                        continue
                    samples.append((_td_seconds(obj.time_offset), (box.left + box.right) / 2, w * h))
    samples.sort(key=lambda x: x[0])
    return samples


def build_crop_plan(
    samples: list[tuple[float, float, float]],
    src_w: int,
    src_h: int,
    duration: float,
    target_aspect: float,
    step: float = 0.2,
) -> CropPlan:
    """Turn raw face samples into smooth crop keyframes.

    For every `step` seconds the largest face within 0.6 s is chosen, then the centre
    is smoothed with a dead zone + exponential moving average so the frame never jitters.
    """
    crop_h = _even(src_h)
    crop_w = _even(min(src_w, crop_h * target_aspect))
    if crop_w >= src_w:  # source is already narrower than the target aspect
        crop_w = _even(src_w)
        crop_h = _even(min(src_h, crop_w / target_aspect))
    y = _even((src_h - crop_h) / 2)
    max_x = src_w - crop_w

    if not samples or max_x <= 0:
        return CropPlan(crop_w, crop_h, y, [(0.0, max(0, max_x // 2))])

    targets: list[float] = []
    last = 0.5
    idx = 0
    n_steps = max(1, int(math.ceil(duration / step)))
    for i in range(n_steps + 1):
        t = i * step
        while idx < len(samples) and samples[idx][0] < t - 0.6:
            idx += 1
        window = [s for s in samples[idx: idx + 64] if abs(s[0] - t) <= 0.6]
        if window:
            last = max(window, key=lambda s: s[2])[1]
        targets.append(last)

    keyframes: list[tuple[float, int]] = []
    smoothed = targets[0]
    dead_zone = 0.04
    alpha = 0.25
    last_x = -10_000
    for i, target in enumerate(targets):
        if abs(target - smoothed) > dead_zone:
            smoothed += alpha * (target - smoothed)
        center_px = smoothed * src_w
        x = int(max(0, min(max_x, center_px - crop_w / 2)))
        if abs(x - last_x) >= 2 or i == 0:
            keyframes.append((round(i * step, 3), x))
            last_x = x
    return CropPlan(crop_w, crop_h, y, keyframes)


async def track_faces(
    segment_gcs_uri: str,
    src_w: int,
    src_h: int,
    duration: float,
    target_aspect: float,
    segment_video: Optional[Path] = None,
) -> CropPlan:
    s = get_settings()
    try:
        if s.is_local:
            from local_providers import detect_faces

            model = Path(s.yunet_model_path)
            if not model.is_absolute():
                model = Path(__file__).resolve().parent / model
            samples = await asyncio.to_thread(detect_faces, segment_video, str(model))
        else:
            samples = await asyncio.to_thread(_faces_sync, segment_gcs_uri)
    except Exception as exc:
        logger.warning("Face tracking failed, using a centred crop: %s", exc)
        samples = []
    return build_crop_plan(samples, src_w, src_h, duration, target_aspect)


def write_crop_commands(plan: CropPlan, path: Path) -> None:
    """sendcmd script that moves the crop window at each keyframe."""
    lines = [f"{t:.3f} crop@face x {x};" for t, x in plan.keyframes]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------------------
# Step 7: Speech-to-Text V2 word timestamps
# --------------------------------------------------------------------------------------


def _transcribe_sync(gcs_uri: str, language_code: str) -> list[Word]:
    from google.cloud.speech_v2.types import cloud_speech

    s = get_settings()
    recognizer = f"projects/{s.gcp_project}/locations/{s.speech_location}/recognizers/_"
    config = cloud_speech.RecognitionConfig(
        auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
        language_codes=[language_code],
        model=s.speech_model,
        features=cloud_speech.RecognitionFeatures(
            enable_word_time_offsets=True,
            enable_automatic_punctuation=True,
        ),
    )
    request = cloud_speech.BatchRecognizeRequest(
        recognizer=recognizer,
        config=config,
        files=[cloud_speech.BatchRecognizeFileMetadata(uri=gcs_uri)],
        recognition_output_config=cloud_speech.RecognitionOutputConfig(
            inline_response_config=cloud_speech.InlineOutputConfig(),
        ),
    )
    operation = speech_client().batch_recognize(request=request)
    response = operation.result(timeout=1200)
    file_result = response.results.get(gcs_uri)
    if file_result is None:
        return []
    if file_result.error and file_result.error.code:
        raise PipelineError(f"Speech-to-Text error: {file_result.error.message}")

    transcript = (
        file_result.inline_result.transcript
        if file_result.inline_result and file_result.inline_result.transcript.results
        else file_result.transcript
    )
    words: list[Word] = []
    for result in transcript.results:
        if not result.alternatives:
            continue
        for w in result.alternatives[0].words:
            text = (w.word or "").strip()
            if not text:
                continue
            start, end = _td_seconds(w.start_offset), _td_seconds(w.end_offset)
            words.append(Word(text=text, start=start, end=max(end, start + 0.05)))
    words.sort(key=lambda w: w.start)
    return words


async def transcribe_words(vocals: Path, workdir: Path, gcs_prefix: str, language_code: str) -> list[Word]:
    s = get_settings()
    if s.is_local:
        from local_providers import whisper_words

        wav = workdir / "speech16k.wav"
        await run_cmd(
            ["ffmpeg", "-hide_banner", "-y", "-i", str(vocals), "-ac", "1", "-ar", "16000", str(wav)],
            timeout=300,
        )
        try:
            raw = await asyncio.to_thread(whisper_words, wav, language_code, s.whisper_model)
        except Exception as exc:
            logger.warning("Whisper failed; rendering without captions: %s", exc)
            return []
        return [Word(text=t, start=a, end=b) for t, a, b in raw]
    flac = workdir / "speech.flac"
    await run_cmd(
        ["ffmpeg", "-hide_banner", "-y", "-i", str(vocals), "-ac", "1", "-ar", "16000", "-c:a", "flac", str(flac)],
        timeout=300,
    )
    uri = await upload_file(flac, f"{gcs_prefix}/speech.flac", "audio/flac")
    try:
        return await asyncio.to_thread(_transcribe_sync, uri, language_code)
    except PipelineError:
        raise
    except Exception as exc:
        logger.warning("Transcription failed; rendering without captions: %s", exc)
        return []


# --------------------------------------------------------------------------------------
# Step 8: animated ASS captions
# --------------------------------------------------------------------------------------


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """#RRGGBB -> ASS &HAABBGGRR."""
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def group_words(words: list[Word], max_words: int = 3, max_span: float = 1.4, max_chars: int = 18) -> list[list[Word]]:
    groups: list[list[Word]] = []
    current: list[Word] = []
    for w in words:
        too_long = current and (
            len(current) >= max_words
            or w.end - current[0].start > max_span
            or sum(len(x.text) + 1 for x in current) + len(w.text) > max_chars
            or w.start - current[-1].end > 0.6
        )
        if too_long:
            groups.append(current)
            current = []
        current.append(w)
        if re.search(r"[.!?…]$", w.text):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def build_ass(words: list[Word], duration: float, highlight_hex: str, font_name: str, center_y: int) -> str:
    """Word-by-word pop captions: each group stays on screen, the spoken word is
    highlighted and scales up briefly (karaoke style)."""
    s = get_settings()
    primary = _ass_color("#FFFFFF")
    highlight = _ass_color(highlight_hex)
    outline = _ass_color("#000000")
    shadow = _ass_color("#000000", 0x60)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {s.out_width}
PlayResY: {s.out_height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font_name},92,{primary},{highlight},{outline},{shadow},-1,0,0,0,100,100,1,0,1,7,3,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    groups = group_words([w for w in words if w.start < duration], max_words=3)
    pos = f"\\an5\\pos({s.out_width // 2},{center_y})"
    for gi, group in enumerate(groups):
        group_end = groups[gi + 1][0].start if gi + 1 < len(groups) else group[-1].end + 0.4
        group_end = min(group_end, group[-1].end + 0.8, duration)
        for wi, word in enumerate(group):
            start = word.start
            end = group[wi + 1].start if wi + 1 < len(group) else group_end
            if end - start < 0.04:
                continue
            parts = []
            for k, other in enumerate(group):
                token = _ass_escape(other.text.upper())
                if k == wi:
                    parts.append(
                        f"{{\\c{highlight}\\fscx115\\fscy115\\t(0,90,\\fscx100\\fscy100)}}{token}{{\\c{primary}\\fscx100\\fscy100}}"
                    )
                else:
                    parts.append(token)
            intro_fx = "\\fad(60,0)" if wi == 0 else ""
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{{{pos}{intro_fx}}}" + " ".join(parts)
            )
    return header + "\n".join(events) + "\n"


# --------------------------------------------------------------------------------------
# Step 9: the multi-layer FFmpeg render
# --------------------------------------------------------------------------------------


@dataclass
class RenderInputs:
    segment_video: Path
    vocals: Path
    broll_a: Path
    broll_b: Path
    card_image: Path
    intro_audio: Path
    outro_audio: Path
    intro_text_file: Path
    outro_text_file: Path
    credit_text_file: Path
    captions_file: Path
    crop_cmd_file: Path
    crop: CropPlan
    font_path: str
    main_duration: float
    intro_duration: float
    outro_duration: float
    accent_hex: str


def build_filtergraph(r: RenderInputs) -> str:
    """Return the full filter_complex.

    Layout (1080x1920):
      intro card  -> Imagen 9:16 still with slow zoom, hook title, AI voiceover
      main        -> top 1080x960 speaker crop that follows the face track
                     bottom 1080x960 Imagen B-roll (two stills, Ken Burns + crossfade)
                     accent divider, word-by-word animated captions over the seam,
                     source credit line
                     audio: demucs vocals, cleaned + loudness-normalised
      outro card  -> same still, zooming out, takeaway text, AI voiceover
    Everything is concatenated in one pass.
    """
    s = get_settings()
    W, H, FPS = s.out_width, s.out_height, s.out_fps
    half = H // 2
    font = r.font_path.replace("\\", "/").replace("'", "")
    accent = "0x" + r.accent_hex.lstrip("#")

    main_d = f"{r.main_duration:.3f}"
    intro_d = f"{r.intro_duration:.3f}"
    outro_d = f"{r.outro_duration:.3f}"
    xfade_d = 0.6
    half_main = r.main_duration / 2
    broll_frames_a = int(math.ceil((half_main + xfade_d) * FPS)) + 2
    broll_frames_b = int(math.ceil((r.main_duration - half_main + xfade_d) * FPS)) + 2
    xfade_offset = max(0.1, half_main - xfade_d / 2)
    intro_frames = int(math.ceil(r.intro_duration * FPS)) + 2
    outro_frames = int(math.ceil(r.outro_duration * FPS)) + 2
    x0 = r.crop.keyframes[0][1] if r.crop.keyframes else 0

    # loudnorm resamples internally (192 kHz), so bring every branch back to 48 kHz stereo.
    loud = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    audio_fmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    common_text = f"fontfile='{font}':expansion=none:fontcolor=white:borderw=6:bordercolor=black@0.85"

    graph = [
        # ---- main: speaker crop (top) ----
        f"[0:v]fps={FPS},setpts=PTS-STARTPTS,settb=AVTB,sendcmd=f={r.crop_cmd_file.name},"
        f"crop@face=w={r.crop.width}:h={r.crop.height}:x={x0}:y={r.crop.y},"
        f"scale={W}:{half}:flags=lanczos,setsar=1,format=yuv420p[top]",
        # ---- main: B-roll (bottom), two Ken Burns stills crossfaded ----
        f"[2:v]scale={int(W * 1.35)}:{int(half * 1.35)}:force_original_aspect_ratio=increase,"
        f"crop={int(W * 1.35)}:{int(half * 1.35)},"
        f"zoompan=z='min(zoom+0.0009,1.30)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={broll_frames_a}:s={W}x{half}:fps={FPS},setsar=1,format=yuv420p,settb=AVTB[bra]",
        f"[3:v]scale={int(W * 1.35)}:{int(half * 1.35)}:force_original_aspect_ratio=increase,"
        f"crop={int(W * 1.35)}:{int(half * 1.35)},"
        f"zoompan=z='if(eq(on,0),1.30,max(zoom-0.0009,1.0))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={broll_frames_b}:s={W}x{half}:fps={FPS},setsar=1,format=yuv420p,settb=AVTB[brb]",
        f"[bra][brb]xfade=transition=smoothleft:duration={xfade_d}:offset={xfade_offset:.3f},"
        f"trim=duration={main_d},setpts=PTS-STARTPTS[bottom]",
        # ---- main: stack, divider, captions, credit ----
        f"[top][bottom]vstack=inputs=2,"
        f"drawbox=x=0:y={half - 5}:w={W}:h=10:color={accent}@0.95:t=fill,"
        f"ass={r.captions_file.name},"
        f"drawtext=textfile={r.credit_text_file.name}:{common_text}:fontsize=30:borderw=3:"
        f"x=(w-text_w)/2:y={half - 62},"
        f"trim=duration={main_d},setpts=PTS-STARTPTS,fps={FPS},format=yuv420p,settb=AVTB[mainv]",
        f"[1:a]{audio_fmt},atrim=duration={main_d},asetpts=PTS-STARTPTS,"
        f"highpass=f=70,lowpass=f=15000,"
        f"acompressor=threshold=-20dB:ratio=3:attack=5:release=150:makeup=2,"
        f"{loud},apad,atrim=duration={main_d}[maina]",
        # ---- title cards ----
        f"[4:v]scale={int(W * 1.25)}:{int(H * 1.25)}:force_original_aspect_ratio=increase,"
        f"crop={int(W * 1.25)}:{int(H * 1.25)},split=2[card1][card2]",
        f"[card1]zoompan=z='min(zoom+0.0012,1.25)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={intro_frames}:s={W}x{H}:fps={FPS},setsar=1,"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.35:t=fill,"
        f"drawbox=x=90:y={int(H * 0.30) - 30}:w=140:h=12:color={accent}@1:t=fill,"
        f"drawtext=textfile={r.intro_text_file.name}:{common_text}:fontsize=96:line_spacing=18:"
        f"x=90:y={int(H * 0.30)}:alpha='min(1,t/0.35)',"
        f"trim=duration={intro_d},setpts=PTS-STARTPTS,format=yuv420p,settb=AVTB[introv]",
        f"[card2]zoompan=z='if(eq(on,0),1.25,max(zoom-0.0012,1.0))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={outro_frames}:s={W}x{H}:fps={FPS},setsar=1,"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.45:t=fill,"
        f"drawtext=textfile={r.outro_text_file.name}:{common_text}:fontsize=72:line_spacing=16:"
        f"x=(w-text_w)/2:y=(h-text_h)/2:alpha='min(1,t/0.35)',"
        f"drawbox=x=(iw-140)/2:y={int(H * 0.5) + 260}:w=140:h=12:color={accent}@1:t=fill,"
        f"trim=duration={outro_d},setpts=PTS-STARTPTS,format=yuv420p,settb=AVTB[outrov]",
        f"[5:a]{audio_fmt},{loud},apad,atrim=duration={intro_d},asetpts=PTS-STARTPTS[introa]",
        f"[6:a]{audio_fmt},{loud},apad,atrim=duration={outro_d},asetpts=PTS-STARTPTS[outroa]",
        # ---- final concat ----
        "[introv][introa][mainv][maina][outrov][outroa]concat=n=3:v=1:a=1[vout][aout]",
    ]
    return ";\n".join(graph)


async def render_short(r: RenderInputs, workdir: Path, out: Path) -> None:
    graph_file = workdir / "filtergraph.txt"
    graph_file.write_text(build_filtergraph(r), encoding="utf-8")
    await run_cmd(
        [
            "ffmpeg", "-hide_banner", "-y",
            "-i", str(r.segment_video),
            "-i", str(r.vocals),
            "-i", str(r.broll_a),
            "-i", str(r.broll_b),
            "-i", str(r.card_image),
            "-i", str(r.intro_audio),
            "-i", str(r.outro_audio),
            "-filter_complex_script", graph_file.name,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high", "-level", "4.2",
            "-pix_fmt", "yuv420p", "-r", str(get_settings().out_fps), "-g", str(get_settings().out_fps * 2),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", str(out),
        ],
        cwd=workdir,
        timeout=3000,
    )


async def make_thumbnail(video: Path, at: float, out: Path) -> None:
    await run_cmd(
        [
            "ffmpeg", "-hide_banner", "-y", "-ss", f"{at:.2f}", "-i", str(video),
            "-frames:v", "1", "-vf", "scale=720:-2", "-q:v", "3", str(out),
        ],
        timeout=120,
    )


# --------------------------------------------------------------------------------------
# Per-clip orchestration
# --------------------------------------------------------------------------------------

ACCENTS = ["#00E5FF", "#FF3D7F", "#7CFF4F", "#FFB020", "#B388FF"]


async def process_clip(
    job_id: str,
    index: int,
    seg: ViralSegment,
    source: SourceVideo,
    opts: JobOptions,
    workdir: Path,
    report: Callable[[float, str], Awaitable[None]],
) -> ClipResult:
    s = get_settings()
    cdir = workdir / f"clip_{index:02d}"
    cdir.mkdir(parents=True, exist_ok=True)
    gcs_prefix = f"jobs/{job_id}/clips/{index:02d}"
    accent = ACCENTS[index % len(ACCENTS)]
    voice = opts.tts_voice or s.tts_voice

    await report(0.05, "Cutting the segment")
    segment_video = cdir / "segment.mp4"
    await cut_segment(source, seg, segment_video)
    main_duration = await ffprobe_duration(segment_video)
    if s.is_local:
        seg_uri_task = asyncio.create_task(asyncio.sleep(0, result=f"local://{gcs_prefix}/segment.mp4"))
    else:
        seg_uri_task = asyncio.create_task(upload_file(segment_video, f"{gcs_prefix}/segment.mp4", "video/mp4"))

    # Independent AI work runs concurrently.
    await report(0.15, "Generating voiceover, B-roll and tracking the speaker")
    intro_wav, outro_wav = cdir / "intro.wav", cdir / "outro.wav"
    broll_a, broll_b, card = cdir / "broll_a.png", cdir / "broll_b.png", cdir / "card.png"

    lang = opts.language_code
    intro_task = asyncio.create_task(synthesize_voiceover(seg.intro_commentary, voice, intro_wav, lang))
    outro_task = asyncio.create_task(synthesize_voiceover(seg.outro_commentary, voice, outro_wav, lang))
    terms = seg.broll_search_terms or ["", "", ""]
    broll_tasks = [
        asyncio.create_task(
            generate_broll(seg.broll_prompts[0], "1:1", broll_a, accent, terms[0], segment_video, main_duration * 0.25)
        ),
        asyncio.create_task(
            generate_broll(seg.broll_prompts[1], "1:1", broll_b, accent, terms[1], segment_video, main_duration * 0.6)
        ),
        asyncio.create_task(
            generate_broll(seg.broll_prompts[2], "9:16", card, accent, terms[2], segment_video, main_duration * 0.1)
        ),
    ]

    seg_uri = await seg_uri_task
    target_aspect = s.out_width / (s.out_height / 2)
    crop_task = asyncio.create_task(
        track_faces(seg_uri, source.width, source.height, main_duration, target_aspect, segment_video)
    )

    await report(0.3, "Isolating the spoken vocals")
    vocals = await isolate_vocals(segment_video, cdir)

    await report(0.55, "Transcribing word timings for captions")
    words = await transcribe_words(vocals, cdir, gcs_prefix, opts.language_code)

    intro_duration, outro_duration = await asyncio.gather(intro_task, outro_task)
    await asyncio.gather(*broll_tasks)
    crop = await crop_task

    # Text layers (files avoid any FFmpeg escaping problems).
    intro_txt = cdir / "intro.txt"
    outro_txt = cdir / "outro.txt"
    credit_txt = cdir / "credit.txt"
    intro_txt.write_text(_wrap(seg.hook_text.upper(), 14, 5), encoding="utf-8")
    outro_txt.write_text(_wrap(seg.outro_commentary, 22, 7), encoding="utf-8")
    credit_txt.write_text(f"Clip from: {source.channel}"[:60], encoding="utf-8")

    font_path = font_for_language(opts.language_code)
    captions = cdir / "captions.ass"
    captions.write_text(
        build_ass(words, main_duration, opts.caption_color, _font_family(font_path), s.out_height // 2 + 150),
        encoding="utf-8",
    )
    crop_cmd = cdir / "crop.cmd"
    write_crop_commands(crop, crop_cmd)

    await report(0.7, "Rendering the Short")
    final = cdir / "final.mp4"
    await render_short(
        RenderInputs(
            segment_video=segment_video,
            vocals=vocals,
            broll_a=broll_a,
            broll_b=broll_b,
            card_image=card,
            intro_audio=intro_wav,
            outro_audio=outro_wav,
            intro_text_file=intro_txt,
            outro_text_file=outro_txt,
            credit_text_file=credit_txt,
            captions_file=captions,
            crop_cmd_file=crop_cmd,
            crop=crop,
            font_path=font_path,
            main_duration=main_duration,
            intro_duration=intro_duration + 0.25,
            outro_duration=outro_duration + 0.6,
            accent_hex=accent,
        ),
        cdir,
        final,
    )

    await report(0.92, "Uploading the finished Short")
    thumb = cdir / "thumb.jpg"
    await make_thumbnail(final, intro_duration + min(2.0, main_duration / 3), thumb)
    total = await ffprobe_duration(final)
    transcript = " ".join(w.text for w in words)

    video_path = f"{gcs_prefix}/short.mp4"
    thumb_path = f"{gcs_prefix}/thumb.jpg"
    captions_path = f"{gcs_prefix}/captions.ass"
    await asyncio.gather(
        upload_file(final, video_path, "video/mp4"),
        upload_file(thumb, thumb_path, "image/jpeg"),
        upload_file(captions, captions_path, "text/plain"),
    )

    description = (
        f"{seg.description.strip()}\n\n"
        f"Original video: {source.title} by {source.channel}\n{source.webpage_url}\n\n"
        + " ".join(f"#{h}" for h in seg.hashtags + ["Shorts"])
    )
    return ClipResult(
        index=index,
        title=seg.title.strip()[:100],
        description=description[:4900],
        hashtags=seg.hashtags,
        viral_score=seg.viral_score,
        reason=seg.reason,
        start_seconds=seg.start_seconds,
        end_seconds=seg.end_seconds,
        duration_seconds=round(total, 2),
        video_gcs_path=video_path,
        thumbnail_gcs_path=thumb_path,
        captions_gcs_path=captions_path,
        transcript=transcript,
    )


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------


async def run_pipeline(
    job_id: str,
    opts: JobOptions,
    progress: ProgressCallback = _noop_progress,
) -> PipelineResult:
    """Run the full pipeline for one job. Raises PipelineError with a readable message."""
    if not opts.rights_confirmed:
        raise PipelineError("Confirm that you own this video or have the rights to clip it")
    url = _sanitize_url(opts.url)

    s = get_settings()
    workdir = Path(s.work_dir) / f"job_{job_id}_{uuid.uuid4().hex[:6]}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        source = await download_source(url, workdir, progress)
        plan = await extract_viral_plan(source, opts, job_id, progress)

        n = len(plan.segments)
        clip_share = 0.78 / n  # progress budget per clip between 20% and 98%

        def make_reporter(i: int) -> Callable[[float, str], Awaitable[None]]:
            async def _report(frac: float, msg: str) -> None:
                await progress("render", 0.2 + clip_share * (i + frac), f"Clip {i + 1}/{n}: {msg}")

            return _report

        clips: list[ClipResult] = []
        for i, seg in enumerate(plan.segments):
            reporter = make_reporter(i)
            clip = await process_clip(job_id, i, seg, source, opts, workdir, reporter)
            clips.append(clip)
            # Free memory-backed /tmp as soon as each clip is uploaded.
            shutil.rmtree(workdir / f"clip_{i:02d}", ignore_errors=True)

        result = PipelineResult(
            job_id=job_id,
            source_title=source.title,
            source_channel=source.channel,
            source_url=source.webpage_url,
            video_summary=plan.video_summary,
            clips=clips,
        )
        await upload_json(_result_to_dict(result), f"jobs/{job_id}/result.json")
        await progress("done", 1.0, f"{len(clips)} Short(s) ready")
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _result_to_dict(result: PipelineResult) -> dict[str, Any]:
    return {
        "job_id": result.job_id,
        "source_title": result.source_title,
        "source_channel": result.source_channel,
        "source_url": result.source_url,
        "video_summary": result.video_summary,
        "clips": [c.__dict__ for c in result.clips],
    }


result_to_dict = _result_to_dict
