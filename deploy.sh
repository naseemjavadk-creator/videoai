#!/usr/bin/env bash
# =============================================================================
# ClipSafe AI — one-shot Google Cloud setup + Cloud Run deployment
#
# What it does (idempotent, safe to re-run):
#   1. Enables every required Google Cloud API
#   2. Creates service accounts with least-privilege IAM roles
#   3. Creates the media bucket with a 24-hour auto-delete lifecycle + CORS
#   4. Creates an Artifact Registry Docker repository
#   5. Stores secrets (OAuth client, session key, optional yt-dlp cookies)
#   6. Builds and deploys the backend (private Cloud Run service)
#   7. Builds and deploys the frontend (public Cloud Run service), if present,
#      or prints the environment variables to paste into Vercel
#
# Usage:
#   export PROJECT_ID=my-gcp-project
#   export REGION=us-central1                       # optional
#   export GOOGLE_OAUTH_CLIENT_ID=xxx.apps.googleusercontent.com      # optional on first run
#   export GOOGLE_OAUTH_CLIENT_SECRET=xxxx                            # optional on first run
#   export APP_PASSWORD='choose-a-dashboard-password'                 # optional; generated if unset
#   ./deploy.sh
#
#   Frontend on Vercel instead of Cloud Run:
#   export FRONTEND_HOST=vercel
#   export APP_URL=https://your-app.vercel.app
#   ./deploy.sh
#
# Flags:
#   --backend-only    Skip the frontend build/deploy
#   --skip-build      Re-deploy the last pushed images without rebuilding
#   --help            Show this help
#
# Use ClipSafe AI only on videos you own or have the rights/licence to clip.
# =============================================================================
set -Eeuo pipefail

# ----------------------------------------------------------------------------
# Pretty logging
# ----------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_OFF=$'\033[0m'
else
  C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_OFF=""
fi
log()  { printf '%s==>%s %s\n' "${C_BLUE}" "${C_OFF}" "$*"; }
ok()   { printf '%s ✓ %s %s\n' "${C_GREEN}" "${C_OFF}" "$*"; }
warn() { printf '%s ! %s %s\n' "${C_YELLOW}" "${C_OFF}" "$*" >&2; }
die()  { printf '%s ✗ %s %s\n' "${C_RED}" "${C_OFF}" "$*" >&2; exit 1; }
trap 'die "Failed at line ${LINENO}: ${BASH_COMMAND}"' ERR

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# ----------------------------------------------------------------------------
# Arguments
# ----------------------------------------------------------------------------
DEPLOY_FRONTEND=true
SKIP_BUILD=false
for arg in "$@"; do
  case "${arg}" in
    --backend-only) DEPLOY_FRONTEND=false ;;
    --skip-build)   SKIP_BUILD=true ;;
    -h|--help)      usage ;;
    *)              die "Unknown flag: ${arg} (see --help)" ;;
  esac
done

# ----------------------------------------------------------------------------
# Configuration (override any of these with environment variables)
# ----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
[[ -n "${PROJECT_ID}" && "${PROJECT_ID}" != "(unset)" ]] || die "Set PROJECT_ID (export PROJECT_ID=your-project-id)."

REGION="${REGION:-us-central1}"
VERTEX_LOCATION="${VERTEX_LOCATION:-us-central1}"
SPEECH_LOCATION="${SPEECH_LOCATION:-us-central1}"
APP_NAME="${APP_NAME:-clipsafe}"

BACKEND_SERVICE="${BACKEND_SERVICE:-${APP_NAME}-api}"
FRONTEND_SERVICE="${FRONTEND_SERVICE:-${APP_NAME}-web}"
AR_REPO="${AR_REPO:-${APP_NAME}}"
BUCKET="${BUCKET:-${PROJECT_ID}-${APP_NAME}-media}"

GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-pro}"
IMAGEN_MODEL="${IMAGEN_MODEL:-imagen-3.0-generate-001}"
TTS_VOICE="${TTS_VOICE:-en-US-Chirp3-HD-Charon}"
SPEECH_MODEL="${SPEECH_MODEL:-chirp_2}"

BACKEND_CPU="${BACKEND_CPU:-4}"
BACKEND_MEMORY="${BACKEND_MEMORY:-16Gi}"
BACKEND_CONCURRENCY="${BACKEND_CONCURRENCY:-2}"
BACKEND_MIN_INSTANCES="${BACKEND_MIN_INSTANCES:-1}"
BACKEND_MAX_INSTANCES="${BACKEND_MAX_INSTANCES:-5}"
FRONTEND_MAX_INSTANCES="${FRONTEND_MAX_INSTANCES:-10}"

# Where the Next.js frontend runs: "cloudrun" (default) or "vercel".
FRONTEND_HOST="${FRONTEND_HOST:-cloudrun}"
[[ "${FRONTEND_HOST}" == "cloudrun" || "${FRONTEND_HOST}" == "vercel" ]] || die "FRONTEND_HOST must be cloudrun or vercel."

YTDLP_COOKIES_FILE="${YTDLP_COOKIES_FILE:-${SCRIPT_DIR}/secrets/youtube_cookies.txt}"

BACKEND_SA_NAME="${APP_NAME}-backend"
FRONTEND_SA_NAME="${APP_NAME}-frontend"
BACKEND_SA="${BACKEND_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
FRONTEND_SA="${FRONTEND_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

SECRET_OAUTH_ID="${APP_NAME}-google-oauth-client-id"
SECRET_OAUTH_SECRET="${APP_NAME}-google-oauth-client-secret"
SECRET_SESSION="${APP_NAME}-session-secret"
SECRET_COOKIES="${APP_NAME}-ytdlp-cookies"
SECRET_API_KEY="${APP_NAME}-backend-api-key"
SECRET_APP_PASSWORD="${APP_NAME}-app-password"

IMAGE_TAG="$(date -u +%Y%m%d-%H%M%S)"
AR_HOST="${REGION}-docker.pkg.dev"
BACKEND_IMAGE_BASE="${AR_HOST}/${PROJECT_ID}/${AR_REPO}/backend"
FRONTEND_IMAGE_BASE="${AR_HOST}/${PROJECT_ID}/${AR_REPO}/frontend"

# ----------------------------------------------------------------------------
# Pre-flight
# ----------------------------------------------------------------------------
command -v gcloud >/dev/null 2>&1 || die "gcloud CLI not found. Install: https://cloud.google.com/sdk/docs/install"
command -v openssl >/dev/null 2>&1 || die "openssl not found (used to generate the session secret)."
[[ -f "${SCRIPT_DIR}/backend/Dockerfile" ]] || die "backend/Dockerfile not found next to deploy.sh."

ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n1)"
[[ -n "${ACTIVE_ACCOUNT}" ]] || die "No active gcloud account. Run: gcloud auth login"

gcloud config set project "${PROJECT_ID}" --quiet >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# Cloud Run's deterministic URL format: https://SERVICE-PROJECT_NUMBER.REGION.run.app
FRONTEND_URL_PREDICTED="https://${FRONTEND_SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
if [[ "${FRONTEND_HOST}" == "vercel" ]]; then
  [[ -n "${APP_URL:-}" ]] || die "FRONTEND_HOST=vercel needs APP_URL (e.g. https://your-app.vercel.app)."
  FRONTEND_URL_PREDICTED="${APP_URL%/}"
  DEPLOY_FRONTEND=false
fi

cat <<EOF

${C_BLUE}ClipSafe AI deployment${C_OFF}
  Account        : ${ACTIVE_ACCOUNT}
  Project        : ${PROJECT_ID} (#${PROJECT_NUMBER})
  Region         : ${REGION}
  Bucket         : gs://${BUCKET}
  Backend        : ${BACKEND_SERVICE}
  Frontend       : ${FRONTEND_HOST} -> ${FRONTEND_URL_PREDICTED}
  Gemini / Imagen: ${GEMINI_MODEL} / ${IMAGEN_MODEL}

EOF

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
sa_exists()     { gcloud iam service-accounts describe "$1" --project "${PROJECT_ID}" >/dev/null 2>&1; }
secret_exists() { gcloud secrets describe "$1" --project "${PROJECT_ID}" >/dev/null 2>&1; }

ensure_sa() {
  local name="$1" display="$2" email="${1}@${PROJECT_ID}.iam.gserviceaccount.com"
  if sa_exists "${email}"; then
    ok "Service account ${email} exists"
  else
    gcloud iam service-accounts create "${name}" --display-name "${display}" --project "${PROJECT_ID}" --quiet
    ok "Created service account ${email}"
    # IAM is eventually consistent; give the new identity a moment to propagate.
    sleep 10
  fi
}

grant_project_role() {
  local member="$1" role="$2"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "${member}" --role "${role}" --condition=None --quiet >/dev/null
  ok "Granted ${role} to ${member}"
}

# Write a secret value from stdin; adds a new version only when the value changed.
upsert_secret() {
  local name="$1" value
  value="$(cat)"
  if ! secret_exists "${name}"; then
    gcloud secrets create "${name}" --replication-policy=automatic --project "${PROJECT_ID}" --quiet >/dev/null
  fi
  local current=""
  current="$(gcloud secrets versions access latest --secret "${name}" --project "${PROJECT_ID}" 2>/dev/null || true)"
  if [[ "${current}" == "${value}" ]]; then
    ok "Secret ${name} unchanged"
  else
    printf '%s' "${value}" | gcloud secrets versions add "${name}" --data-file=- --project "${PROJECT_ID}" --quiet >/dev/null
    ok "Secret ${name} updated"
  fi
}

grant_secret_access() {
  local secret="$1" sa="$2"
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member "serviceAccount:${sa}" --role roles/secretmanager.secretAccessor \
    --project "${PROJECT_ID}" --quiet >/dev/null
}

latest_image() {
  # Prints the most recently pushed image (by tag) for the given image path, or nothing.
  gcloud artifacts docker images list "$1" --include-tags \
    --sort-by=~UPDATE_TIME --limit=1 --format='value(format("{0}@{1}",package,version))' 2>/dev/null || true
}

# ----------------------------------------------------------------------------
# 1. Enable APIs
# ----------------------------------------------------------------------------
log "Enabling Google Cloud APIs (this can take a few minutes on a new project)"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  texttospeech.googleapis.com \
  videointelligence.googleapis.com \
  speech.googleapis.com \
  storage.googleapis.com \
  youtube.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  logging.googleapis.com \
  --project "${PROJECT_ID}" --quiet
ok "APIs enabled"

# ----------------------------------------------------------------------------
# 2. Service accounts + IAM
# ----------------------------------------------------------------------------
log "Configuring service accounts"
ensure_sa "${BACKEND_SA_NAME}"  "ClipSafe AI backend (pipeline)"
ensure_sa "${FRONTEND_SA_NAME}" "ClipSafe AI frontend (Next.js)"

# Backend: Vertex AI (Gemini + Imagen), Speech-to-Text V2, logging, API quota usage.
# Text-to-Speech and Video Intelligence need only an enabled API + serviceUsageConsumer.
for role in \
  roles/aiplatform.user \
  roles/speech.client \
  roles/logging.logWriter \
  roles/serviceusage.serviceUsageConsumer; do
  grant_project_role "serviceAccount:${BACKEND_SA}" "${role}"
done

# Backend signs V4 URLs with the IAM signBlob API (no key files on disk).
gcloud iam service-accounts add-iam-policy-binding "${BACKEND_SA}" \
  --member "serviceAccount:${BACKEND_SA}" \
  --role roles/iam.serviceAccountTokenCreator \
  --project "${PROJECT_ID}" --quiet >/dev/null
ok "Backend can sign its own URLs (serviceAccountTokenCreator on itself)"

grant_project_role "serviceAccount:${FRONTEND_SA}" roles/logging.logWriter

# Cloud Build (default compute SA on new projects) must push images and write logs.
for role in roles/artifactregistry.writer roles/logging.logWriter roles/storage.objectViewer; do
  grant_project_role "serviceAccount:${COMPUTE_SA}" "${role}"
done

# ----------------------------------------------------------------------------
# 3. Media bucket with 24h lifecycle
# ----------------------------------------------------------------------------
log "Configuring bucket gs://${BUCKET}"
if gcloud storage buckets describe "gs://${BUCKET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  ok "Bucket exists"
else
  gcloud storage buckets create "gs://${BUCKET}" \
    --project "${PROJECT_ID}" \
    --location "${REGION}" \
    --default-storage-class STANDARD \
    --uniform-bucket-level-access \
    --public-access-prevention \
    --quiet
  ok "Created bucket"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

# GCS lifecycle ages are whole days: age=1 deletes objects once they are 24h+ old
# (GCS applies lifecycle actions asynchronously, usually within a day of eligibility).
cat > "${TMP_DIR}/lifecycle.json" <<'JSON'
{
  "rule": [
    { "action": { "type": "Delete" }, "condition": { "age": 1 } },
    { "action": { "type": "AbortIncompleteMultipartUpload" }, "condition": { "age": 1 } }
  ]
}
JSON

# Browsers stream finished clips straight from signed URLs (read-only, time-limited).
cat > "${TMP_DIR}/cors.json" <<'JSON'
[
  {
    "origin": ["*"],
    "method": ["GET", "HEAD"],
    "responseHeader": ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Range"],
    "maxAgeSeconds": 3600
  }
]
JSON

gcloud storage buckets update "gs://${BUCKET}" \
  --lifecycle-file "${TMP_DIR}/lifecycle.json" \
  --cors-file "${TMP_DIR}/cors.json" \
  --clear-soft-delete \
  --project "${PROJECT_ID}" --quiet
ok "24-hour auto-delete lifecycle and CORS applied (soft delete off, so deleted media is really gone)"

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member "serviceAccount:${BACKEND_SA}" --role roles/storage.objectAdmin \
  --project "${PROJECT_ID}" --quiet >/dev/null
ok "Backend has objectAdmin on the bucket"

# ----------------------------------------------------------------------------
# 4. Artifact Registry
# ----------------------------------------------------------------------------
log "Configuring Artifact Registry repository ${AR_REPO}"
if gcloud artifacts repositories describe "${AR_REPO}" --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  ok "Repository exists"
else
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format docker --location "${REGION}" \
    --description "ClipSafe AI container images" \
    --project "${PROJECT_ID}" --quiet
  ok "Created repository"
fi

# Keep the 5 newest images per package and delete untagged leftovers after 7 days.
cat > "${TMP_DIR}/ar-cleanup.json" <<'JSON'
[
  { "name": "keep-recent", "action": { "type": "Keep" }, "mostRecentVersions": { "keepCount": 5 } },
  { "name": "delete-untagged", "action": { "type": "Delete" },
    "condition": { "tagState": "UNTAGGED", "olderThan": "7d" } }
]
JSON
gcloud artifacts repositories set-cleanup-policies "${AR_REPO}" \
  --location "${REGION}" --policy "${TMP_DIR}/ar-cleanup.json" \
  --project "${PROJECT_ID}" --quiet >/dev/null
ok "Image cleanup policy set"

# ----------------------------------------------------------------------------
# 5. Secrets
# ----------------------------------------------------------------------------
log "Configuring secrets"

if ! secret_exists "${SECRET_SESSION}"; then
  openssl rand -base64 48 | tr -d '\n' | upsert_secret "${SECRET_SESSION}"
else
  ok "Secret ${SECRET_SESSION} exists (kept so signed-in sessions stay valid)"
fi
grant_secret_access "${SECRET_SESSION}" "${FRONTEND_SA}"

# Shared key the frontend sends in X-API-Key. The backend checks it on every call,
# in addition to Cloud Run IAM when the frontend also runs on Cloud Run.
if ! secret_exists "${SECRET_API_KEY}"; then
  openssl rand -hex 32 | tr -d '\n' | upsert_secret "${SECRET_API_KEY}"
else
  ok "Secret ${SECRET_API_KEY} exists"
fi
grant_secret_access "${SECRET_API_KEY}" "${BACKEND_SA}"
grant_secret_access "${SECRET_API_KEY}" "${FRONTEND_SA}"

# Dashboard login password (the public frontend is password-protected so nobody
# else can start jobs on your Google Cloud bill).
if [[ -n "${APP_PASSWORD:-}" ]]; then
  printf '%s' "${APP_PASSWORD}" | upsert_secret "${SECRET_APP_PASSWORD}"
elif ! secret_exists "${SECRET_APP_PASSWORD}"; then
  openssl rand -base64 18 | tr -d '\n/+=' | upsert_secret "${SECRET_APP_PASSWORD}"
  ok "Generated a dashboard password (shown at the end)"
fi
grant_secret_access "${SECRET_APP_PASSWORD}" "${FRONTEND_SA}"

OAUTH_READY=false
if [[ -n "${GOOGLE_OAUTH_CLIENT_ID:-}" && -n "${GOOGLE_OAUTH_CLIENT_SECRET:-}" ]]; then
  printf '%s' "${GOOGLE_OAUTH_CLIENT_ID}"     | upsert_secret "${SECRET_OAUTH_ID}"
  printf '%s' "${GOOGLE_OAUTH_CLIENT_SECRET}" | upsert_secret "${SECRET_OAUTH_SECRET}"
fi
if secret_exists "${SECRET_OAUTH_ID}" && secret_exists "${SECRET_OAUTH_SECRET}"; then
  grant_secret_access "${SECRET_OAUTH_ID}"     "${FRONTEND_SA}"
  grant_secret_access "${SECRET_OAUTH_SECRET}" "${FRONTEND_SA}"
  OAUTH_READY=true
  ok "YouTube OAuth client configured"
else
  warn "No YouTube OAuth client yet. The app will deploy, but 'Upload to YouTube' stays disabled."
  warn "Create one (APIs & Services > Credentials > OAuth client ID > Web application) with redirect URI:"
  warn "    ${FRONTEND_URL_PREDICTED}/api/youtube/callback"
  warn "then re-run with GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET exported."
fi

COOKIES_READY=false
if [[ -f "${YTDLP_COOKIES_FILE}" ]]; then
  upsert_secret "${SECRET_COOKIES}" < "${YTDLP_COOKIES_FILE}"
fi
if secret_exists "${SECRET_COOKIES}"; then
  grant_secret_access "${SECRET_COOKIES}" "${BACKEND_SA}"
  COOKIES_READY=true
  ok "yt-dlp cookies secret will be mounted into the backend"
fi

# ----------------------------------------------------------------------------
# 6. Backend: build + deploy
# ----------------------------------------------------------------------------
if [[ "${SKIP_BUILD}" == true ]]; then
  BACKEND_IMAGE="$(latest_image "${BACKEND_IMAGE_BASE}")"
  [[ -n "${BACKEND_IMAGE}" ]] || die "--skip-build set but no backend image exists yet."
  ok "Re-using ${BACKEND_IMAGE}"
else
  BACKEND_IMAGE="${BACKEND_IMAGE_BASE}:${IMAGE_TAG}"
  log "Building backend image with Cloud Build (first build ~15 min: PyTorch + demucs weights)"
  gcloud builds submit "${SCRIPT_DIR}/backend" \
    --tag "${BACKEND_IMAGE}" \
    --machine-type e2-highcpu-8 \
    --timeout 3600s \
    --region "${REGION}" \
    --project "${PROJECT_ID}" --quiet
  ok "Pushed ${BACKEND_IMAGE}"
fi

BACKEND_ENV="GCP_PROJECT=${PROJECT_ID},GCS_BUCKET=${BUCKET},VERTEX_LOCATION=${VERTEX_LOCATION}"
BACKEND_ENV+=",SPEECH_LOCATION=${SPEECH_LOCATION},SPEECH_MODEL=${SPEECH_MODEL}"
BACKEND_ENV+=",GEMINI_MODEL=${GEMINI_MODEL},IMAGEN_MODEL=${IMAGEN_MODEL},TTS_VOICE=${TTS_VOICE}"
BACKEND_ENV+=",SERVICE_ACCOUNT_EMAIL=${BACKEND_SA},SIGNED_URL_TTL_SECONDS=21600"
BACKEND_ENV+=",MAX_SOURCE_DURATION_SECONDS=3600,ALLOWED_ORIGINS=${FRONTEND_URL_PREDICTED}"
if [[ "${COOKIES_READY}" == true ]]; then
  BACKEND_ENV+=",YTDLP_COOKIES_PATH=/secrets/ytdlp/cookies.txt"
fi

BACKEND_SECRETS="API_KEY=${SECRET_API_KEY}:latest"
if [[ "${COOKIES_READY}" == true ]]; then
  BACKEND_SECRETS+=",/secrets/ytdlp/cookies.txt=${SECRET_COOKIES}:latest"
fi

# Vercel cannot present a Google ID token, so in that mode the backend accepts
# unauthenticated traffic at the Cloud Run layer and relies on the API key.
if [[ "${FRONTEND_HOST}" == "vercel" ]]; then
  BACKEND_AUTH_FLAG="--allow-unauthenticated"
else
  BACKEND_AUTH_FLAG="--no-allow-unauthenticated"
fi

BACKEND_DEPLOY_ARGS=(
  --image "${BACKEND_IMAGE}"
  --region "${REGION}"
  --service-account "${BACKEND_SA}"
  "${BACKEND_AUTH_FLAG}"
  --execution-environment gen2
  --cpu "${BACKEND_CPU}"
  --memory "${BACKEND_MEMORY}"
  --no-cpu-throttling
  --cpu-boost
  --concurrency "${BACKEND_CONCURRENCY}"
  --min-instances "${BACKEND_MIN_INSTANCES}"
  --max-instances "${BACKEND_MAX_INSTANCES}"
  --timeout 3600
  --port 8080
  --set-env-vars "${BACKEND_ENV}"
  --set-secrets "${BACKEND_SECRETS}"
  --labels "app=${APP_NAME},component=backend"
  --project "${PROJECT_ID}"
  --quiet
)
log "Deploying backend service ${BACKEND_SERVICE} (${BACKEND_AUTH_FLAG#--})"
gcloud run deploy "${BACKEND_SERVICE}" "${BACKEND_DEPLOY_ARGS[@]}"
BACKEND_URL="$(gcloud run services describe "${BACKEND_SERVICE}" --region "${REGION}" --project "${PROJECT_ID}" --format='value(status.url)')"
ok "Backend live at ${BACKEND_URL}"

# Only the frontend's identity may call the backend.
gcloud run services add-iam-policy-binding "${BACKEND_SERVICE}" \
  --region "${REGION}" \
  --member "serviceAccount:${FRONTEND_SA}" --role roles/run.invoker \
  --project "${PROJECT_ID}" --quiet >/dev/null
ok "Frontend service account can invoke the backend"

# ----------------------------------------------------------------------------
# 7. Frontend: build + deploy
# ----------------------------------------------------------------------------
FRONTEND_URL=""
if [[ "${DEPLOY_FRONTEND}" == true ]]; then
  if [[ ! -f "${SCRIPT_DIR}/frontend/Dockerfile" ]]; then
    warn "frontend/Dockerfile not found yet (it arrives in Phase 4). Skipping frontend deploy."
  else
    if [[ "${SKIP_BUILD}" == true ]]; then
      FRONTEND_IMAGE="$(latest_image "${FRONTEND_IMAGE_BASE}")"
      [[ -n "${FRONTEND_IMAGE}" ]] || die "--skip-build set but no frontend image exists yet."
      ok "Re-using ${FRONTEND_IMAGE}"
    else
      FRONTEND_IMAGE="${FRONTEND_IMAGE_BASE}:${IMAGE_TAG}"
      log "Building frontend image with Cloud Build"
      gcloud builds submit "${SCRIPT_DIR}/frontend" \
        --tag "${FRONTEND_IMAGE}" \
        --machine-type e2-highcpu-8 \
        --timeout 1800s \
        --region "${REGION}" \
        --project "${PROJECT_ID}" --quiet
      ok "Pushed ${FRONTEND_IMAGE}"
    fi

    FRONTEND_SECRETS="SESSION_SECRET=${SECRET_SESSION}:latest,BACKEND_API_KEY=${SECRET_API_KEY}:latest"
    FRONTEND_SECRETS+=",APP_PASSWORD=${SECRET_APP_PASSWORD}:latest"
    if [[ "${OAUTH_READY}" == true ]]; then
      FRONTEND_SECRETS+=",GOOGLE_OAUTH_CLIENT_ID=${SECRET_OAUTH_ID}:latest"
      FRONTEND_SECRETS+=",GOOGLE_OAUTH_CLIENT_SECRET=${SECRET_OAUTH_SECRET}:latest"
    fi

    log "Deploying frontend service ${FRONTEND_SERVICE} (public)"
    gcloud run deploy "${FRONTEND_SERVICE}" \
      --image "${FRONTEND_IMAGE}" \
      --region "${REGION}" \
      --service-account "${FRONTEND_SA}" \
      --allow-unauthenticated \
      --cpu 1 \
      --memory 1Gi \
      --concurrency 80 \
      --min-instances 0 \
      --max-instances "${FRONTEND_MAX_INSTANCES}" \
      --timeout 300 \
      --port 3000 \
      --set-env-vars "BACKEND_URL=${BACKEND_URL},APP_URL=${FRONTEND_URL_PREDICTED},NODE_ENV=production" \
      --set-secrets "${FRONTEND_SECRETS}" \
      --labels "app=${APP_NAME},component=frontend" \
      --project "${PROJECT_ID}" --quiet

    FRONTEND_URL="$(gcloud run services describe "${FRONTEND_SERVICE}" --region "${REGION}" --project "${PROJECT_ID}" --format='value(status.url)')"
    ok "Frontend live at ${FRONTEND_URL}"
  fi
fi

# ----------------------------------------------------------------------------
# Vercel: print the values to add under Project Settings > Environment Variables
# ----------------------------------------------------------------------------
if [[ "${FRONTEND_HOST}" == "vercel" ]]; then
  VERCEL_ENV_FILE="${SCRIPT_DIR}/secrets/vercel.env"
  mkdir -p "${SCRIPT_DIR}/secrets"
  chmod 700 "${SCRIPT_DIR}/secrets"
  {
    echo "BACKEND_URL=${BACKEND_URL}"
    echo "BACKEND_API_KEY=$(gcloud secrets versions access latest --secret "${SECRET_API_KEY}" --project "${PROJECT_ID}")"
    echo "SESSION_SECRET=$(gcloud secrets versions access latest --secret "${SECRET_SESSION}" --project "${PROJECT_ID}")"
    echo "APP_URL=${FRONTEND_URL_PREDICTED}"
    echo "APP_PASSWORD=$(gcloud secrets versions access latest --secret "${SECRET_APP_PASSWORD}" --project "${PROJECT_ID}")"
    if [[ "${OAUTH_READY}" == true ]]; then
      echo "GOOGLE_OAUTH_CLIENT_ID=$(gcloud secrets versions access latest --secret "${SECRET_OAUTH_ID}" --project "${PROJECT_ID}")"
      echo "GOOGLE_OAUTH_CLIENT_SECRET=$(gcloud secrets versions access latest --secret "${SECRET_OAUTH_SECRET}" --project "${PROJECT_ID}")"
    fi
  } > "${VERCEL_ENV_FILE}"
  chmod 600 "${VERCEL_ENV_FILE}"
  FRONTEND_URL="${FRONTEND_URL_PREDICTED} (deploy from Vercel)"
  ok "Vercel environment variables written to ${VERCEL_ENV_FILE} (git-ignored; do not commit)"
  warn "Add them in Vercel > Project > Settings > Environment Variables, or: vercel env add <NAME> production"
fi

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
cat <<EOF

${C_GREEN}Deployment complete.${C_OFF}
  Backend           : ${BACKEND_URL}
  Frontend          : ${FRONTEND_URL:-not deployed}
  Media bucket      : gs://${BUCKET}  (objects auto-delete after 24h)
  OAuth redirect URI: ${FRONTEND_URL_PREDICTED}/api/youtube/callback
  YouTube upload    : $([[ "${OAUTH_READY}" == true ]] && echo enabled || echo "disabled until an OAuth client is configured")
  Dashboard password: $(gcloud secrets versions access latest --secret "${SECRET_APP_PASSWORD}" --project "${PROJECT_ID}")

Test the backend from your machine:
  curl -H "Authorization: Bearer \$(gcloud auth print-identity-token)" ${BACKEND_URL}/healthz

Reminder: process only videos you own or have the rights to clip.
EOF
