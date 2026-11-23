#!/usr/bin/env bash
# ClipSafe AI background control: start | stop | status
# Used by the "ClipSafe AI" and "Stop ClipSafe" apps. Runs without a Terminal window;
# logs go to data/logs/.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="${ROOT}/data/run"
LOGS="${ROOT}/data/logs"
mkdir -p "${RUN}" "${LOGS}"

for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [[ -x "$p" ]] && eval "$("$p" shellenv)" && break
done
NODE_BIN="$(brew --prefix node@22 2>/dev/null)/bin"
export PATH="${NODE_BIN}:/usr/sbin:/usr/bin:/bin:${PATH}"

healthy() {
  curl -fsS -m 3 http://127.0.0.1:8080/healthz >/dev/null 2>&1 &&
    curl -fsS -m 5 -o /dev/null http://127.0.0.1:3000/ 2>/dev/null
}

kill_port() {
  local pids
  pids="$(lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -n "${pids}" ]] && kill ${pids} 2>/dev/null
  sleep 1
  pids="$(lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -n "${pids}" ]] && kill -9 ${pids} 2>/dev/null
  return 0
}

stop() {
  for f in "${RUN}"/*.pid; do
    [[ -f "$f" ]] || continue
    pid="$(cat "$f")"
    [[ -n "${pid}" ]] && pkill -TERM -P "${pid}" 2>/dev/null; kill "${pid}" 2>/dev/null
    rm -f "$f"
  done
  kill_port 8080
  kill_port 3000
}

start() {
  if [[ ! -x "${ROOT}/backend/.venv/bin/python" || ! -f "${ROOT}/backend/.env" || ! -d "${ROOT}/frontend/.next" ]]; then
    echo "ClipSafe is not installed yet. Run the installer first." >&2
    exit 1
  fi
  if healthy; then
    return 0
  fi
  stop  # clear anything half-running or stale on ports 8080/3000

  (cd "${ROOT}/backend" && nohup ./.venv/bin/python -m uvicorn main:app \
      --host 127.0.0.1 --port 8080 --log-level warning >>"${LOGS}/backend.log" 2>&1 </dev/null &
    echo $! >"${RUN}/backend.pid")
  (cd "${ROOT}/frontend" && NEXT_TELEMETRY_DISABLED=1 nohup npx next start -H 127.0.0.1 -p 3000 \
      >>"${LOGS}/frontend.log" 2>&1 </dev/null &
    echo $! >"${RUN}/frontend.pid")

  for _ in $(seq 1 90); do
    if healthy; then
      return 0
    fi
    sleep 1
  done
  echo "ClipSafe did not start in time. Last backend log lines:" >&2
  tail -n 8 "${LOGS}/backend.log" >&2
  exit 1
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) if healthy; then echo running; else echo stopped; fi ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
