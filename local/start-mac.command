#!/usr/bin/env bash
# ClipSafe AI: start the local app (backend on :8080, dashboard on :3000).
# Double-click this file in Finder, or run: bash local/start-mac.command
# Close this window (or press Ctrl+C) to stop ClipSafe.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [[ -x "$p" ]] && eval "$("$p" shellenv)" && break
done
NODE_BIN="$(brew --prefix node@22 2>/dev/null)/bin"
export PATH="${NODE_BIN}:${PATH}"

if [[ ! -x "${ROOT}/backend/.venv/bin/python" || ! -f "${ROOT}/backend/.env" || ! -d "${ROOT}/frontend/.next" ]]; then
  echo "ClipSafe is not installed yet. Run: bash \"${ROOT}/local/install-mac.sh\""
  read -r -p "Press Enter to close." _
  exit 1
fi

for port in 8080 3000; do
  if lsof -nP -iTCP:${port} -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port ${port} is already in use. Is ClipSafe already running in another window?"
    read -r -p "Press Enter to close." _
    exit 1
  fi
done

mkdir -p "${ROOT}/data/logs"
cleanup() {
  echo
  echo "Stopping ClipSafe..."
  [[ -n "${API_PID:-}" ]] && kill "${API_PID}" 2>/dev/null
  [[ -n "${WEB_PID:-}" ]] && kill "${WEB_PID}" 2>/dev/null
  wait 2>/dev/null
  echo "Stopped."
}
trap cleanup EXIT INT TERM

echo "Starting ClipSafe AI..."
(
  cd "${ROOT}/backend" && exec ./.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8080 --log-level warning
) > >(tee -a "${ROOT}/data/logs/backend.log") 2>&1 &
API_PID=$!
(
  cd "${ROOT}/frontend" && NEXT_TELEMETRY_DISABLED=1 exec npx next start -H 127.0.0.1 -p 3000
) >> "${ROOT}/data/logs/frontend.log" 2>&1 &
WEB_PID=$!

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1 && curl -fsS -o /dev/null http://127.0.0.1:3000/ 2>/dev/null; then
    break
  fi
  if ! kill -0 "${API_PID}" 2>/dev/null; then
    echo "The backend stopped. Last lines of data/logs/backend.log:"
    tail -n 20 "${ROOT}/data/logs/backend.log"
    read -r -p "Press Enter to close." _
    exit 1
  fi
  sleep 1
done

open "http://localhost:3000"
echo
echo "ClipSafe AI is running at http://localhost:3000"
echo "Keep this window open while you make Shorts. Close it (or press Ctrl+C) to stop."
wait
