#!/usr/bin/env bash
# Update ClipSafe AI on your Mac: stops the running app, installs any new packages,
# rebuilds the dashboard and starts it again. Your keys in backend/.env are kept.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

echo "==> Stopping ClipSafe if it is running"
bash "${ROOT}/local/clipsafe-ctl.sh" stop 2>/dev/null || true
pkill -f "uvicorn main:app" 2>/dev/null || true
pkill -f "next start" 2>/dev/null || true
pkill -f "next-server" 2>/dev/null || true
sleep 1

echo "==> Updating packages (a few minutes)"
CLIPSAFE_UPDATE=1 bash "${ROOT}/local/install-mac.sh"

chmod +x "${ROOT}/local/"*.sh "${ROOT}/local/start-mac.command" 2>/dev/null || true
xattr -d com.apple.quarantine "${ROOT}/local/start-mac.command" 2>/dev/null || true

bash "${ROOT}/local/make-app-mac.sh" || echo "(Could not create the Dock app; you can still run: bash local/start-mac.command)"

echo "==> Starting ClipSafe in the background"
bash "${ROOT}/local/clipsafe-ctl.sh" start && open "http://localhost:3000"
echo "Done. Next time just click the ClipSafe AI icon in the Dock. You can close this window."
