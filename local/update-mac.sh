#!/usr/bin/env bash
# Update ClipSafe AI on your Mac: stops the running app, installs any new packages,
# rebuilds the dashboard and starts it again. Your keys in backend/.env are kept.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

echo "==> Stopping ClipSafe if it is running"
pkill -f "uvicorn main:app" 2>/dev/null || true
pkill -f "next start" 2>/dev/null || true
pkill -f "next-server" 2>/dev/null || true
sleep 1

echo "==> Updating packages (a few minutes)"
CLIPSAFE_UPDATE=1 bash "${ROOT}/local/install-mac.sh"

echo "==> Starting ClipSafe"
chmod +x "${ROOT}/local/start-mac.command"
open "${ROOT}/local/start-mac.command"
echo "Done. ClipSafe opens in your browser in a few seconds."
