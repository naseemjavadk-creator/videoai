# ClipSafe AI

Turn a long YouTube video **that you own or have the rights to clip** into vertical
Shorts: Gemini picks the strongest moments, then the pipeline adds original AI
commentary, AI B-roll split-screen, speaker-tracked 9:16 framing and animated captions,
and you can upload the result straight to your own YouTube channel.

## Rights and responsible use

- Use ClipSafe AI only on videos **you own**, or that you have a licence or written
  permission to clip and republish. Downloading other people's videos from YouTube is
  against YouTube's Terms of Service unless the owner permits it.
- The transformations (commentary, B-roll, captions, reframing) make your Shorts more
  original and more useful to viewers. They do **not** turn someone else's content into
  yours, and they are not a substitute for permission.
- The `demucs` step separates speech from background music so you can drop a music bed
  you have no licence to republish (for example, a track that was licensed only for the
  original upload) and keep the spoken audio. It is **not** a way to evade Content ID
  or any other rights-management system; matched claims should be respected or
  disputed through YouTube's normal process.

## Project structure

```
clipsafe-ai/
├── README.md
├── deploy.sh                       # APIs, IAM, bucket (24h lifecycle), secrets, build + Cloud Run deploy
├── backend/                        # Python 3.11 · FastAPI · FFmpeg · yt-dlp · demucs
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── pipeline.py                 # async media + AI pipeline and the multi-layer FFmpeg filtergraph
│   └── main.py                     # API server, worker queue, job status, V4 signed URLs
└── frontend/                       # Next.js 14 (App Router) · TypeScript · Tailwind · Lucide
    ├── Dockerfile                  # standalone image for Cloud Run (port 3000)
    ├── middleware.ts               # password gate for the whole dashboard
    ├── app/
    │   ├── page.tsx                # dark-mode creator dashboard
    │   ├── login/page.tsx
    │   └── api/
    │       ├── login/route.ts
    │       ├── jobs/route.ts, jobs/[jobId]/route.ts       # proxy to the backend
    │       └── youtube/{auth,callback,status,disconnect,upload}/route.ts
    ├── components/VideoPreviewCard.tsx
    └── lib/{backend,session,youtube,types}.ts
```

### What each Short looks like

1. **Intro card**: an Imagen 3 still (slow zoom), the hook text, and your AI commentary voiceover.
2. **Main segment**: the speaker on top (face-tracked crop) and Imagen B-roll on the bottom
   (Ken Burns + crossfade), an accent divider, word-by-word animated captions, a source credit
   line, and the demucs-isolated spoken audio, loudness-normalised to -14 LUFS.
3. **Outro card**: your AI takeaway or question to viewers, voiced by Cloud TTS.

The YouTube description that ClipSafe writes always credits and links the original video.

## Architecture

```
Browser ──► clipsafe-web (Next.js, public Cloud Run)
               │  ID-token-authenticated calls (run.invoker)
               ▼
            clipsafe-api (FastAPI, private Cloud Run, 4 vCPU / 16 GiB)
               ├─ yt-dlp ............ fetch the source video
               ├─ Gemini 2.5 Pro .... pick viral segments + write commentary (JSON)
               ├─ demucs ............ isolate vocals
               ├─ Cloud TTS ......... Chirp 3 HD commentary voiceover
               ├─ Imagen 3 .......... original B-roll images
               ├─ Video Intelligence  face tracks for 9:16 crop
               ├─ Speech-to-Text V2 . word timestamps for captions
               └─ FFmpeg ............ final multi-layer render
                      │
                      ▼
            gs://<project>-clipsafe-media  (private, signed URLs, auto-delete after 24h)
```

Job status is stored as JSON in the bucket, so it survives instance restarts and any
backend instance can answer a status request.

## What `deploy.sh` sets up

| Step | Resource |
|------|----------|
| APIs | Cloud Run, Cloud Build, Artifact Registry, Vertex AI, Text-to-Speech, Video Intelligence, Speech-to-Text, Cloud Storage, YouTube Data API v3, Secret Manager, IAM, IAM Credentials, Logging |
| Service accounts | `clipsafe-backend` (Vertex AI user, Speech client, bucket objectAdmin, signs its own URLs) and `clipsafe-frontend` (may only invoke the backend and read its secrets) |
| Bucket | `gs://<project>-clipsafe-media`: uniform access, public access blocked, soft delete off, **objects deleted once 24 hours old**, read-only CORS for video playback |
| Artifact Registry | `clipsafe` Docker repo, keeps the 5 newest images |
| Secrets | session key, backend API key, dashboard password (generated unless you set one), YouTube OAuth client ID/secret (from your env), optional yt-dlp cookies |
| Cloud Run | `clipsafe-api` (private, gen2, no CPU throttling, 60-minute timeout, 1 warm instance) and `clipsafe-web` (public) |

GCS lifecycle rules work in whole days, so "age 1" means an object becomes eligible for
deletion when it is 24 hours old; Google applies the deletion asynchronously, normally
within the following day.

## Deploying

Prerequisites: a Google Cloud project with billing enabled, the
[gcloud CLI](https://cloud.google.com/sdk/docs/install), and `openssl`. You can also run
everything from [Cloud Shell](https://shell.cloud.google.com), which has both.

```bash
gcloud auth login
export PROJECT_ID=your-project-id
export REGION=us-central1
chmod +x deploy.sh
./deploy.sh
```

The first backend build takes about 15 minutes (PyTorch and the demucs model are baked
into the image). Later runs reuse cached layers.

### YouTube upload (OAuth)

1. Run `./deploy.sh` once. It prints the OAuth redirect URI, which looks like
   `https://clipsafe-web-<PROJECT_NUMBER>.<REGION>.run.app/api/youtube/callback`.
2. In the Cloud Console open **APIs & Services > OAuth consent screen**, configure it
   (External, add yourself as a test user) and add the scope
   `https://www.googleapis.com/auth/youtube.upload`.
3. Open **APIs & Services > Credentials > Create credentials > OAuth client ID**, choose
   **Web application**, and add the redirect URI from step 1.
4. Re-run with the client details:

```bash
export GOOGLE_OAUTH_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
export GOOGLE_OAUTH_CLIENT_SECRET=xxxxxxxx
./deploy.sh
```

### Dashboard password

The frontend is public, so it is protected by a password (otherwise anyone who finds the
URL could run jobs on your Google Cloud bill). Set your own with
`export APP_PASSWORD='...'` before running `deploy.sh`, or let the script generate one; it
prints the password at the end and stores it in Secret Manager.

### Frontend on Vercel instead of Cloud Run

The backend must stay on Cloud Run (FFmpeg, PyTorch and 60-minute jobs do not fit in
serverless functions), but the Next.js frontend can run on Vercel:

```bash
export FRONTEND_HOST=vercel
export APP_URL=https://your-app.vercel.app     # your Vercel production URL
./deploy.sh
```

This deploys only the backend, makes it reachable from Vercel (protected by an API key
instead of Cloud Run IAM), and writes every value Vercel needs to `secrets/vercel.env`.
Then in Vercel: import the repository, set **Root Directory** to `frontend`, paste the
variables from `secrets/vercel.env` into **Settings > Environment Variables**, and deploy.
Use `https://your-app.vercel.app/api/youtube/callback` as the OAuth redirect URI.

### Optional: yt-dlp cookies

YouTube sometimes rate-limits or challenges downloads from cloud IP ranges. If that
happens with your own videos, export cookies from a browser signed in to **your own**
account in Netscape format, save them as `secrets/youtube_cookies.txt`, and re-run
`deploy.sh`. They are stored in Secret Manager and mounted read-only into the backend.
Never commit that file.

### Useful flags and overrides

```bash
./deploy.sh --backend-only        # deploy only the API
./deploy.sh --skip-build          # redeploy the latest images with new settings
BACKEND_MIN_INSTANCES=0 ./deploy.sh   # scale the API to zero when idle (cheaper, slower first job)
TTS_VOICE=en-US-Chirp3-HD-Aoede ./deploy.sh
```

### Costs

The warm backend instance (4 vCPU, 16 GiB, no CPU throttling) is the main fixed cost.
Set `BACKEND_MIN_INSTANCES=0` if you process clips only occasionally. Gemini, Imagen,
TTS, Speech-to-Text and Video Intelligence are billed per use.

## Local development

```bash
# Backend (needs gcloud auth application-default login, ffmpeg and deno on PATH)
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GCP_PROJECT=your-project GCS_BUCKET=your-project-clipsafe-media \
       SERVICE_ACCOUNT_EMAIL=clipsafe-backend@your-project.iam.gserviceaccount.com \
       WORK_DIR=/tmp/clipsafe API_KEY=dev-key
uvicorn main:app --reload --port 8080

# Frontend
cd frontend
cp .env.example .env.local   # BACKEND_URL=http://localhost:8080, BACKEND_API_KEY=dev-key, ...
npm install
npm run dev
```

Signing URLs locally uses the IAM signBlob API, so your own account needs
`roles/iam.serviceAccountTokenCreator` on the backend service account.

## Frontend environment variables

| Variable | Purpose |
|----------|---------|
| `BACKEND_URL` | Backend Cloud Run URL |
| `BACKEND_API_KEY` | Shared key sent as `X-API-Key` |
| `APP_URL` | Public URL of the frontend (OAuth redirect base) |
| `SESSION_SECRET` | 32+ random characters for encrypted cookies |
| `APP_PASSWORD` | Dashboard login password |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | YouTube OAuth web client |

## Known limits

- YouTube can block downloads from cloud IP ranges; see the cookies section above.
- Speech-to-Text `chirp_2` and Chirp 3 HD voices cover many languages, but not every
  language/voice pair. If a job fails at the voiceover or caption step for your language,
  pick English commentary or another voice.
- `imagen-3.0-generate-001` and other model IDs can be retired by Google; change
  `IMAGEN_MODEL` / `GEMINI_MODEL` and re-run `./deploy.sh --skip-build`.
- Each YouTube upload costs about 1,600 units of the default 10,000 daily API quota (about 6 uploads per day).

## Backend environment variables

Set by `deploy.sh`; listed here for local development.

| Variable | Default | Purpose |
|----------|---------|---------|
| `GCP_PROJECT` | (required) | Project for all Google Cloud clients |
| `GCS_BUCKET` | (required) | Media + job-status bucket |
| `VERTEX_LOCATION` | `us-central1` | Vertex AI region for Gemini and Imagen |
| `SPEECH_LOCATION` | `us-central1` | Speech-to-Text V2 region |
| `SPEECH_MODEL` | `chirp_2` | Speech-to-Text V2 model |
| `GEMINI_MODEL` | `gemini-2.5-pro` | Segment selection + commentary |
| `IMAGEN_MODEL` | `imagen-3.0-generate-001` | B-roll images |
| `TTS_VOICE` | `en-US-Chirp3-HD-Charon` | Commentary voice |
| `SERVICE_ACCOUNT_EMAIL` | backend SA | Used to sign V4 URLs via IAM |
| `SIGNED_URL_TTL_SECONDS` | `21600` | Signed URL lifetime (6 h) |
| `MAX_SOURCE_DURATION_SECONDS` | `3600` | Reject longer source videos |
| `ALLOWED_ORIGINS` | frontend URL | CORS allow-list |
| `API_KEY` | from Secret Manager | Required `X-API-Key` header value |
| `MAX_CONCURRENT_JOBS` | `1` | Jobs processed at once per instance |
| `YTDLP_COOKIES_PATH` | unset | Path to mounted cookies file |
| `WORK_DIR` | `/tmp/clipsafe` | Scratch space (in-memory on Cloud Run) |
| `DEMUCS_MODEL` | `htdemucs` | Vocal separation model |
