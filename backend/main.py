"""
ClipSafe AI API server (FastAPI on Cloud Run).

- POST /api/jobs                 queue a new job (YouTube URL + options)
- GET  /api/jobs                 recent jobs
- GET  /api/jobs/{job_id}        status, progress and finished clips with signed URLs
- DELETE /api/jobs/{job_id}      cancel a job and delete its media
- GET  /api/jobs/{job_id}/clips/{index}/url   fresh signed URL (for YouTube upload)
- GET  /healthz                  liveness probe

Jobs run in an in-process asyncio worker pool. Their state is persisted as JSON in the
media bucket, so any instance can answer status requests and state survives restarts.
The bucket's lifecycle rule deletes everything (media and job state) after 24 hours.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import storage
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl

from pipeline import JobOptions, PipelineError, get_settings, result_to_dict, run_pipeline, storage_client

# --------------------------------------------------------------------------------------
# Logging: one JSON object per line so Cloud Logging parses severity and fields.
# --------------------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        if record.exc_info:
            payload["stack_trace"] = self.formatException(record.exc_info)
        job_id = getattr(record, "job_id", None)
        if job_id:
            payload["job_id"] = job_id
        return json.dumps(payload, ensure_ascii=False)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
logger = logging.getLogger("clipsafe.api")

settings = get_settings()

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TERMINAL: set[str] = {"succeeded", "failed", "cancelled"}
STALE_AFTER_SECONDS = 20 * 60  # a running job with no heartbeat for this long is dead
MAX_QUEUE = 25

# --------------------------------------------------------------------------------------
# API models
# --------------------------------------------------------------------------------------


class CreateJobRequest(BaseModel):
    url: HttpUrl
    max_clips: int = Field(default=3, ge=1, le=5)
    min_clip_seconds: int = Field(default=20, ge=10, le=90)
    max_clip_seconds: int = Field(default=50, ge=15, le=120)
    language_code: str = Field(default="en-US", min_length=2, max_length=16, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
    tts_voice: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z]{2,3}-[A-Za-z0-9-]+$")
    commentary_style: str = Field(default="energetic, insightful creator", max_length=200)
    caption_color: str = Field(default="#FFE14D", pattern=r"^#[0-9A-Fa-f]{6}$")
    rights_confirmed: bool


class CreateJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    position: int


class ClipOut(BaseModel):
    index: int
    title: str
    description: str
    hashtags: list[str]
    viral_score: int
    reason: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    transcript: str
    video_url: str
    download_url: str
    thumbnail_url: str
    expires_at: str


class JobOut(BaseModel):
    job_id: str
    status: JobStatus
    stage: str
    progress: float
    message: str
    error: Optional[str] = None
    created_at: str
    updated_at: str
    url: str
    source_title: Optional[str] = None
    source_channel: Optional[str] = None
    video_summary: Optional[str] = None
    clips: list[ClipOut] = []


class JobSummary(BaseModel):
    job_id: str
    status: JobStatus
    stage: str
    progress: float
    message: str
    created_at: str
    url: str
    source_title: Optional[str] = None
    clip_count: int = 0


class SignedUrlOut(BaseModel):
    url: str
    expires_at: str
    content_type: str
    size_bytes: int
    title: str
    description: str
    hashtags: list[str]


# --------------------------------------------------------------------------------------
# Job store (GCS JSON documents + in-memory cache)
# --------------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, bucket_name: str) -> None:
        self.bucket_name = bucket_name
        self._cache: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def bucket(self) -> storage.Bucket:
        return storage_client().bucket(self.bucket_name)

    @staticmethod
    def _path(job_id: str) -> str:
        return f"jobs/{job_id}/status.json"

    def _lock(self, job_id: str) -> asyncio.Lock:
        return self._locks.setdefault(job_id, asyncio.Lock())

    async def _write(self, doc: dict[str, Any]) -> None:
        data = json.dumps(doc, ensure_ascii=False)

        def _do() -> None:
            blob = self.bucket.blob(self._path(doc["job_id"]))
            blob.cache_control = "no-store"
            blob.upload_from_string(data, content_type="application/json")

        await asyncio.to_thread(_do)

    async def create(self, job_id: str, req: CreateJobRequest) -> dict[str, Any]:
        doc = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "progress": 0.0,
            "message": "Waiting for a free worker",
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
            "heartbeat": time.time(),
            "request": json.loads(req.model_dump_json()),
            "result": None,
        }
        async with self._lock(job_id):
            self._cache[job_id] = doc
            await self._write(doc)
        return doc

    async def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        async with self._lock(job_id):
            doc = self._cache.get(job_id) or await self._read(job_id)
            if doc is None:
                raise KeyError(job_id)
            if doc.get("status") in TERMINAL and fields.get("status") not in TERMINAL:
                return doc  # never resurrect a finished/cancelled job
            doc.update(fields)
            doc["updated_at"] = _now()
            doc["heartbeat"] = time.time()
            self._cache[job_id] = doc
            await self._write(doc)
            return doc

    async def _read(self, job_id: str) -> Optional[dict[str, Any]]:
        def _do() -> Optional[dict[str, Any]]:
            blob = self.bucket.blob(self._path(job_id))
            try:
                return json.loads(blob.download_as_bytes())
            except Exception as exc:  # NotFound or bad JSON
                if exc.__class__.__name__ != "NotFound":
                    logger.warning("Could not read job %s: %s", job_id, exc)
                return None

        return await asyncio.to_thread(_do)

    async def get(self, job_id: str) -> Optional[dict[str, Any]]:
        cached = self._cache.get(job_id)
        if cached and cached["status"] in TERMINAL:
            return cached
        # Another instance may own this job, so read through to GCS for live state.
        doc = await self._read(job_id)
        if doc is None:
            return cached
        if doc["status"] in {"queued", "running"} and job_id not in RUNNING_HERE:
            if time.time() - float(doc.get("heartbeat", 0)) > STALE_AFTER_SECONDS:
                doc["status"] = "failed"
                doc["error"] = "The worker processing this job stopped. Please start it again."
                doc["message"] = doc["error"]
        return doc

    async def list_recent(self, limit: int) -> list[dict[str, Any]]:
        def _do() -> list[dict[str, Any]]:
            blobs = list(
                storage_client().list_blobs(self.bucket_name, prefix="jobs/", match_glob="jobs/*/status.json")
            )
            blobs.sort(key=lambda b: b.time_created or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            out: list[dict[str, Any]] = []
            for blob in blobs[:limit]:
                try:
                    out.append(json.loads(blob.download_as_bytes()))
                except Exception as exc:
                    logger.warning("Skipping unreadable job doc %s: %s", blob.name, exc)
            return out

        return await asyncio.to_thread(_do)

    async def delete_media(self, job_id: str) -> int:
        def _do() -> int:
            count = 0
            for blob in storage_client().list_blobs(self.bucket_name, prefix=f"jobs/{job_id}/"):
                if blob.name.endswith("status.json"):
                    continue
                blob.delete()
                count += 1
            return count

        return await asyncio.to_thread(_do)


store = JobStore(settings.gcs_bucket)

# --------------------------------------------------------------------------------------
# Signed URLs (V4, signed through the IAM signBlob API: no key file needed)
# --------------------------------------------------------------------------------------

_credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
_cred_lock = asyncio.Lock()


async def _fresh_token() -> Optional[str]:
    async with _cred_lock:
        if not _credentials.valid:
            await asyncio.to_thread(_credentials.refresh, GoogleAuthRequest())
        return getattr(_credentials, "token", None)


async def sign_url(gcs_path: str, *, download_name: Optional[str] = None) -> tuple[str, str]:
    ttl = timedelta(seconds=settings.signed_url_ttl_seconds)
    expires_at = (datetime.now(timezone.utc) + ttl).isoformat()
    token = await _fresh_token()
    sa_email = settings.service_account_email or getattr(_credentials, "service_account_email", None)

    def _do() -> str:
        blob = storage_client().bucket(settings.gcs_bucket).blob(gcs_path)
        kwargs: dict[str, Any] = {"version": "v4", "expiration": ttl, "method": "GET"}
        if download_name:
            safe = "".join(c for c in download_name if c.isalnum() or c in " -_.").strip() or "clip"
            kwargs["response_disposition"] = f'attachment; filename="{safe[:80]}.mp4"'
        if not hasattr(_credentials, "sign_bytes"):
            # Compute Engine / Cloud Run metadata credentials: sign via IAM.
            kwargs["service_account_email"] = sa_email
            kwargs["access_token"] = token
        return blob.generate_signed_url(**kwargs)

    return await asyncio.to_thread(_do), expires_at


async def build_job_out(doc: dict[str, Any]) -> JobOut:
    result = doc.get("result") or {}
    clips: list[ClipOut] = []
    if doc.get("status") == "succeeded":
        for clip in result.get("clips", []):
            (video_url, expires_at), (download_url, _), (thumb_url, _) = await asyncio.gather(
                sign_url(clip["video_gcs_path"]),
                sign_url(clip["video_gcs_path"], download_name=clip["title"]),
                sign_url(clip["thumbnail_gcs_path"]),
            )
            clips.append(
                ClipOut(
                    index=clip["index"],
                    title=clip["title"],
                    description=clip["description"],
                    hashtags=clip["hashtags"],
                    viral_score=clip["viral_score"],
                    reason=clip["reason"],
                    start_seconds=clip["start_seconds"],
                    end_seconds=clip["end_seconds"],
                    duration_seconds=clip["duration_seconds"],
                    transcript=clip.get("transcript", ""),
                    video_url=video_url,
                    download_url=download_url,
                    thumbnail_url=thumb_url,
                    expires_at=expires_at,
                )
            )
    return JobOut(
        job_id=doc["job_id"],
        status=doc["status"],
        stage=doc.get("stage", ""),
        progress=round(float(doc.get("progress", 0.0)), 4),
        message=doc.get("message", ""),
        error=doc.get("error"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
        url=doc.get("request", {}).get("url", ""),
        source_title=result.get("source_title"),
        source_channel=result.get("source_channel"),
        video_summary=result.get("video_summary"),
        clips=clips,
    )


# --------------------------------------------------------------------------------------
# Worker pool
# --------------------------------------------------------------------------------------

QUEUE: "asyncio.Queue[str]" = asyncio.Queue(maxsize=MAX_QUEUE)
RUNNING_HERE: dict[str, asyncio.Task[Any]] = {}
CANCELLED: set[str] = set()


async def _execute(job_id: str) -> None:
    doc = await store.get(job_id)
    if doc is None or doc["status"] in TERMINAL or job_id in CANCELLED:
        return
    opts = JobOptions(**doc["request"])
    log = logging.LoggerAdapter(logger, {"job_id": job_id})

    last_write = 0.0

    async def progress(stage: str, pct: float, message: str) -> None:
        nonlocal last_write
        # Throttle GCS writes to at most one every 2 s, except stage changes and completion.
        now = time.time()
        if stage != (store._cache.get(job_id) or {}).get("stage") or pct >= 1.0 or now - last_write > 2:
            last_write = now
            await store.update(job_id, status="running", stage=stage, progress=min(0.99, pct), message=message)
        log.info("%s %.0f%% %s", stage, pct * 100, message)

    await store.update(job_id, status="running", stage="starting", progress=0.01, message="Starting")
    log.info("job started for %s", opts.url)
    started = time.time()
    try:
        result = await run_pipeline(job_id, opts, progress)
        await store.update(
            job_id,
            status="succeeded",
            stage="done",
            progress=1.0,
            message=f"{len(result.clips)} Short(s) ready",
            result=result_to_dict(result),
        )
        log.info("job succeeded in %.0fs", time.time() - started)
    except asyncio.CancelledError:
        await store.update(job_id, status="cancelled", stage="cancelled", message="Cancelled")
        log.info("job cancelled")
        raise
    except PipelineError as exc:
        await store.update(job_id, status="failed", stage="failed", message=str(exc), error=str(exc))
        log.warning("job failed: %s", exc)
    except Exception as exc:  # unexpected: keep details in logs, show a clean message
        log.exception("job crashed")
        msg = "Something went wrong while processing this video. Please try again."
        await store.update(job_id, status="failed", stage="failed", message=msg, error=f"{msg} ({type(exc).__name__})")


async def worker(n: int) -> None:
    while True:
        job_id = await QUEUE.get()
        try:
            if job_id in CANCELLED:
                continue
            task = asyncio.create_task(_execute(job_id), name=f"job-{job_id}")
            RUNNING_HERE[job_id] = task
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # the worker itself is shutting down
                # otherwise only this job was cancelled (DELETE /api/jobs/{id}); keep serving
        except Exception:
            logger.exception("worker %d crashed on job %s", n, job_id)
        finally:
            RUNNING_HERE.pop(job_id, None)
            CANCELLED.discard(job_id)
            QUEUE.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    workers = [asyncio.create_task(worker(i), name=f"worker-{i}") for i in range(max(1, settings.max_concurrent_jobs))]
    logger.info("ClipSafe API ready with %d worker(s)", len(workers))
    try:
        yield
    finally:
        for job_id, task in list(RUNNING_HERE.items()):
            task.cancel()
            try:
                await store.update(
                    job_id, status="failed", stage="failed",
                    message="The server restarted during processing. Please start the job again.",
                    error="Server shutdown",
                )
            except Exception:
                logger.exception("could not mark job %s as failed during shutdown", job_id)
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


# --------------------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------------------

app = FastAPI(
    title="ClipSafe AI API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if not settings.api_key else None,
    redoc_url=None,
)

origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)


async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    expected = settings.api_key
    if not expected:
        return  # private Cloud Run service: IAM already authenticated the caller
    if not x_api_key or not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, Any]:
    return {"ok": True, "queued": QUEUE.qsize(), "running": len(RUNNING_HERE)}


@app.post(
    "/api/jobs",
    response_model=CreateJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def create_job(req: CreateJobRequest) -> CreateJobResponse:
    if not req.rights_confirmed:
        raise HTTPException(
            status_code=422,
            detail="Please confirm you own this video or have the rights to clip and republish it.",
        )
    url = str(req.url)
    host = (req.url.host or "").lower()
    if not (host == "youtu.be" or host.endswith("youtube.com")):
        raise HTTPException(status_code=422, detail="Only YouTube video URLs are supported.")
    if req.max_clip_seconds <= req.min_clip_seconds:
        raise HTTPException(status_code=422, detail="Maximum clip length must be longer than the minimum.")
    if QUEUE.full():
        raise HTTPException(status_code=429, detail="The queue is full. Please try again in a few minutes.")

    job_id = uuid.uuid4().hex[:12]
    await store.create(job_id, req)
    position = QUEUE.qsize() + len(RUNNING_HERE)
    QUEUE.put_nowait(job_id)
    logger.info("queued job %s for %s (position %d)", job_id, url, position)
    return CreateJobResponse(job_id=job_id, status="queued", position=position)


@app.get("/api/jobs", response_model=list[JobSummary], dependencies=[Depends(require_api_key)])
async def list_jobs(limit: int = Query(default=20, ge=1, le=100)) -> list[JobSummary]:
    docs = await store.list_recent(limit)
    out: list[JobSummary] = []
    for doc in docs:
        result = doc.get("result") or {}
        out.append(
            JobSummary(
                job_id=doc["job_id"],
                status=doc["status"],
                stage=doc.get("stage", ""),
                progress=float(doc.get("progress", 0)),
                message=doc.get("message", ""),
                created_at=doc["created_at"],
                url=doc.get("request", {}).get("url", ""),
                source_title=result.get("source_title"),
                clip_count=len(result.get("clips", [])),
            )
        )
    return out


@app.get("/api/jobs/{job_id}", response_model=JobOut, dependencies=[Depends(require_api_key)])
async def get_job(job_id: str) -> JobOut:
    if not job_id.isalnum() or len(job_id) > 32:
        raise HTTPException(status_code=404, detail="Job not found")
    doc = await store.get(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Job not found (jobs are deleted after 24 hours)")
    return await build_job_out(doc)


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def delete_job(job_id: str) -> dict[str, Any]:
    if not job_id.isalnum() or len(job_id) > 32:
        raise HTTPException(status_code=404, detail="Job not found")
    doc = await store.get(job_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Job not found")
    CANCELLED.add(job_id)
    task = RUNNING_HERE.get(job_id)
    if task:
        task.cancel()
    if doc["status"] not in TERMINAL:
        await store.update(job_id, status="cancelled", stage="cancelled", message="Cancelled")
    deleted = await store.delete_media(job_id)
    return {"job_id": job_id, "deleted_objects": deleted}


@app.get(
    "/api/jobs/{job_id}/clips/{index}/url",
    response_model=SignedUrlOut,
    dependencies=[Depends(require_api_key)],
)
async def clip_url(job_id: str, index: int) -> SignedUrlOut:
    if not job_id.isalnum() or len(job_id) > 32:
        raise HTTPException(status_code=404, detail="Job not found")
    doc = await store.get(job_id)
    if doc is None or doc.get("status") != "succeeded":
        raise HTTPException(status_code=404, detail="Clip not ready")
    clips = (doc.get("result") or {}).get("clips", [])
    clip = next((c for c in clips if int(c["index"]) == index), None)
    if clip is None:
        raise HTTPException(status_code=404, detail="Clip not found")

    def _size() -> int:
        blob = storage_client().bucket(settings.gcs_bucket).get_blob(clip["video_gcs_path"])
        if blob is None:
            raise FileNotFoundError(clip["video_gcs_path"])
        return int(blob.size or 0)

    try:
        size = await asyncio.to_thread(_size)
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail="This clip has expired (media is kept for 24 hours)")
    url, expires_at = await sign_url(clip["video_gcs_path"])
    return SignedUrlOut(
        url=url,
        expires_at=expires_at,
        content_type="video/mp4",
        size_bytes=size,
        title=clip["title"],
        description=clip["description"],
        hashtags=clip["hashtags"],
    )
