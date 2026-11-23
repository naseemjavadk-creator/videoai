#!/usr/bin/env bash
# =============================================================================
# ClipSafe AI: free local install for macOS (Apple Silicon or Intel)
#
# Installs everything ClipSafe needs on this Mac and configures it to use only
# free services: a free Google AI Studio key (Gemini), edge-tts, Whisper, OpenCV
# and Pexels. No Google Cloud account or payment card is needed.
#
#   bash local/install-mac.sh
#
# Safe to run again (for updates or to change your keys).
# Use ClipSafe AI only on videos you own or have the rights to clip.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="${ROOT}/backend"
FRONTEND="${ROOT}/frontend"
DATA="${ROOT}/data"

if [[ -t 1 ]]; then
  B=$'\033[1;34m'; G=$'\033[1;32m'; Y=$'\033[1;33m'; R=$'\033[1;31m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; N=""
fi
step() { printf '\n%s==> %s%s\n' "${B}" "$*" "${N}"; }
ok()   { printf '%s ✓ %s%s\n' "${G}" "$*" "${N}"; }
warn() { printf '%s ! %s%s\n' "${Y}" "$*" "${N}" >&2; }
die()  { printf '%s ✗ %s%s\n' "${R}" "$*" "${N}" >&2; exit 1; }
trap 'die "Install stopped at line ${LINENO}. Copy the messages above and send them to Claude."' ERR

[[ "$(uname -s)" == "Darwin" ]] || die "This installer is for macOS."
ARCH="$(uname -m)"
MACOS="$(sw_vers -productVersion)"
printf '%sClipSafe AI local installer%s  (macOS %s, %s)\n' "${B}" "${N}" "${MACOS}" "${ARCH}"
printf 'Folder: %s\n' "${ROOT}"

# -----------------------------------------------------------------------------
step "1/7 Homebrew (macOS package manager)"
if ! command -v brew >/dev/null 2>&1; then
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [[ -x "$p" ]] && eval "$("$p" shellenv)" && break
  done
fi
if ! command -v brew >/dev/null 2>&1; then
  warn "Homebrew is not installed. Installing it now (it will ask for your Mac password)."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [[ -x "$p" ]] && eval "$("$p" shellenv)" && break
  done
fi
command -v brew >/dev/null 2>&1 || die "Homebrew install failed. Open https://brew.sh and follow the steps, then run this again."
ok "Homebrew $(brew --version | head -n1 | awk '{print $2}')"

# -----------------------------------------------------------------------------
step "2/7 Python 3.11, FFmpeg, Deno, Node.js (this can take 5-15 minutes the first time)"
export HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_ENV_HINTS=1
brew install python@3.11 ffmpeg deno node@22
PY="$(brew --prefix python@3.11)/bin/python3.11"
NODE_BIN="$(brew --prefix node@22)/bin"
export PATH="${NODE_BIN}:${PATH}"
[[ -x "${PY}" ]] || die "Python 3.11 not found after install."
ok "$(${PY} --version), ffmpeg $(ffmpeg -version | head -n1 | awk '{print $3}'), deno $(deno --version | head -n1 | awk '{print $2}'), node $(node --version)"

# -----------------------------------------------------------------------------
step "3/7 Python packages (Gemini, demucs, Whisper, OpenCV, yt-dlp)"
if [[ ! -x "${BACKEND}/.venv/bin/python" ]]; then
  "${PY}" -m venv "${BACKEND}/.venv"
fi
VPY="${BACKEND}/.venv/bin/python"
"${VPY}" -m pip install --upgrade --quiet pip setuptools wheel
"${VPY}" -m pip install --upgrade -r "${BACKEND}/requirements-local.txt"
ok "Python packages installed"

# -----------------------------------------------------------------------------
step "4/7 AI models (face detector, vocal separator, speech recognizer)"
mkdir -p "${BACKEND}/models"
YUNET="${BACKEND}/models/face_detection_yunet_2023mar.onnx"
if [[ ! -s "${YUNET}" ]]; then
  curl -fL --retry 3 -o "${YUNET}" \
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
    || warn "Face model download failed; ClipSafe will use OpenCV's built-in detector instead."
fi
"${VPY}" - <<'PYEOF'
from demucs.pretrained import get_model
get_model("htdemucs")
print(" ✓ demucs htdemucs ready")
PYEOF
WHISPER_MODEL="$(grep -E '^WHISPER_MODEL=' "${BACKEND}/.env" 2>/dev/null | cut -d= -f2- || true)"
WHISPER_MODEL="${WHISPER_MODEL:-small}"
"${VPY}" - "${WHISPER_MODEL}" <<'PYEOF'
import sys
from faster_whisper import WhisperModel
WhisperModel(sys.argv[1], device="cpu", compute_type="int8")
print(f" ✓ whisper {sys.argv[1]} ready")
PYEOF

# -----------------------------------------------------------------------------
step "5/7 Your keys"
mkdir -p "${DATA}/tmp"
ENV_FILE="${BACKEND}/.env"
current() { grep -E "^$1=" "${ENV_FILE}" 2>/dev/null | head -n1 | cut -d= -f2- || true; }

GEMINI_API_KEY="${GEMINI_API_KEY:-$(current GEMINI_API_KEY)}"
if [[ -z "${GEMINI_API_KEY}" ]]; then
  echo "Get a FREE Gemini key: open https://aistudio.google.com/apikey , sign in, click 'Create API key', copy it."
  open "https://aistudio.google.com/apikey" 2>/dev/null || true
  while [[ -z "${GEMINI_API_KEY}" ]]; do
    read -r -p "Paste your Gemini API key here and press Enter: " GEMINI_API_KEY </dev/tty
    GEMINI_API_KEY="$(printf '%s' "${GEMINI_API_KEY}" | tr -d '[:space:]')"
  done
fi

PEXELS_API_KEY="${PEXELS_API_KEY:-$(current PEXELS_API_KEY)}"
if [[ -z "${PEXELS_API_KEY}" && -z "${CLIPSAFE_UPDATE:-}" ]]; then
  echo
  echo "Optional: a FREE Pexels key gives your Shorts real stock-photo B-roll."
  echo "Get one at https://www.pexels.com/api/ (sign up, 'Your API Key'). Press Enter to skip."
  read -r -p "Pexels API key (or Enter to skip): " PEXELS_API_KEY </dev/tty || true
  PEXELS_API_KEY="$(printf '%s' "${PEXELS_API_KEY}" | tr -d '[:space:]')"
fi

API_KEY="$(current API_KEY)"; API_KEY="${API_KEY:-$(openssl rand -hex 24)}"

cat > "${ENV_FILE}" <<ENVEOF
MODE=local
GEMINI_API_KEY=${GEMINI_API_KEY}
GEMINI_MODEL=$(current GEMINI_MODEL | grep -v "^gemini-2\." | grep . || echo gemini-3.8-flash)
GEMINI_FALLBACK_MODEL=$(current GEMINI_FALLBACK_MODEL | grep -v "^gemini-2\." | grep . || echo gemini-3.5-flash-lite)
PEXELS_API_KEY=${PEXELS_API_KEY}
WHISPER_MODEL=${WHISPER_MODEL}
API_KEY=${API_KEY}
DATA_DIR=${DATA}
WORK_DIR=${DATA}/tmp
PUBLIC_BASE_URL=http://127.0.0.1:8080
ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
YUNET_MODEL_PATH=${YUNET}
RETENTION_HOURS=24
ENVEOF
chmod 600 "${ENV_FILE}"
ok "Saved backend settings"

FENV="${FRONTEND}/.env.local"
fcurrent() { grep -E "^$1=" "${FENV}" 2>/dev/null | head -n1 | cut -d= -f2- || true; }
SESSION_SECRET="$(fcurrent SESSION_SECRET)"; SESSION_SECRET="${SESSION_SECRET:-$(openssl rand -hex 32)}"
cat > "${FENV}" <<ENVEOF
BACKEND_URL=http://127.0.0.1:8080
BACKEND_API_KEY=${API_KEY}
APP_URL=http://localhost:3000
SESSION_SECRET=${SESSION_SECRET}
AUTH_DISABLED=1
GOOGLE_OAUTH_CLIENT_ID=${GOOGLE_OAUTH_CLIENT_ID:-$(fcurrent GOOGLE_OAUTH_CLIENT_ID)}
GOOGLE_OAUTH_CLIENT_SECRET=${GOOGLE_OAUTH_CLIENT_SECRET:-$(fcurrent GOOGLE_OAUTH_CLIENT_SECRET)}
ENVEOF
chmod 600 "${FENV}"
ok "Saved frontend settings"

# -----------------------------------------------------------------------------
step "6/7 Checking your Gemini key"
if "${VPY}" - "${GEMINI_API_KEY}" <<'PYEOF'
import sys
from google import genai
client = genai.Client(api_key=sys.argv[1])
r = client.models.generate_content(model="gemini-3.5-flash-lite", contents="Reply with the single word: ready")
print(" ✓ Gemini says:", (r.text or "").strip()[:40])
PYEOF
then :; else
  warn "The Gemini key did not work. Check it at https://aistudio.google.com/apikey and run this installer again."
fi

# -----------------------------------------------------------------------------
step "7/7 Building the dashboard"
cd "${FRONTEND}"
if [[ -f package-lock.json ]]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
NEXT_TELEMETRY_DISABLED=1 npm run build
chmod +x "${ROOT}/local/start-mac.command" "${ROOT}/local/install-mac.sh"
ok "Dashboard built"

cat <<DONE

${G}ClipSafe AI is installed.${N}

Start it any time by double-clicking:
    ${ROOT}/local/start-mac.command
(or run: bash local/start-mac.command)

Your browser will open http://localhost:3000
Finished Shorts are kept in ${DATA} for 24 hours.
DONE
