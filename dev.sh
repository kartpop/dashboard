#!/usr/bin/env bash
# Local dev helper: spin the backend (:8010) and frontend (:5173) up/down in the background.
#
#   ./dev.sh up       # migrate, then start both (logs in .dev/)
#   ./dev.sh down     # stop both
#   ./dev.sh restart
#   ./dev.sh status
#   ./dev.sh logs [backend|frontend]   # tail -f (both if omitted)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT/.dev"
BACKEND_PORT=8010
FRONTEND_PORT=5173

mkdir -p "$RUN_DIR"

port_pids() { lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null || true; }

is_up() { [[ -n "$(port_pids "$1")" ]]; }

wait_for_port() {
  local name=$1 port=$2
  for _ in $(seq 1 30); do
    if is_up "$port"; then echo "  $name up on :$port"; return 0; fi
    sleep 0.5
  done
  echo "  $name did not come up on :$port — see $RUN_DIR/$name.log" >&2
  tail -n 20 "$RUN_DIR/$name.log" >&2 || true
  return 1
}

start_backend() {
  if is_up "$BACKEND_PORT"; then echo "  backend already running on :$BACKEND_PORT"; return; fi
  echo "  backend: running migrations"
  (cd "$ROOT/backend" && uv run alembic upgrade head) >"$RUN_DIR/backend.log" 2>&1 \
    || { echo "  migrations failed — see $RUN_DIR/backend.log" >&2; tail -n 20 "$RUN_DIR/backend.log" >&2; return 1; }
  (cd "$ROOT/backend" && nohup uv run uvicorn app.main:app --reload --port "$BACKEND_PORT" \
    >>"$RUN_DIR/backend.log" 2>&1 & echo $! >"$RUN_DIR/backend.pid")
  wait_for_port backend "$BACKEND_PORT"
}

start_frontend() {
  if is_up "$FRONTEND_PORT"; then echo "  frontend already running on :$FRONTEND_PORT"; return; fi
  if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
    echo "  frontend: npm install"
    (cd "$ROOT/frontend" && npm install) >"$RUN_DIR/frontend.log" 2>&1
  fi
  (cd "$ROOT/frontend" && nohup npm run dev -- --port "$FRONTEND_PORT" --strictPort \
    >>"$RUN_DIR/frontend.log" 2>&1 & echo $! >"$RUN_DIR/frontend.pid")
  wait_for_port frontend "$FRONTEND_PORT"
}

stop_one() {
  local name=$1 port=$2 pidfile="$RUN_DIR/$1.pid"
  if [[ -f "$pidfile" ]]; then
    local pid; pid=$(cat "$pidfile")
    # Kill the wrapper's children (uvicorn reloader workers / vite) then the wrapper itself.
    pkill -TERM -P "$pid" 2>/dev/null || true
    kill -TERM "$pid" 2>/dev/null || true
    rm -f "$pidfile"
  fi
  # Anything still holding the port (e.g. started outside this script).
  local left; left=$(port_pids "$port")
  if [[ -n "$left" ]]; then
    kill -TERM $left 2>/dev/null || true
    sleep 1
    left=$(port_pids "$port")
    [[ -n "$left" ]] && kill -KILL $left 2>/dev/null || true
  fi
  echo "  $name stopped"
}

status() {
  for pair in "backend:$BACKEND_PORT" "frontend:$FRONTEND_PORT"; do
    local name=${pair%%:*} port=${pair##*:}
    if is_up "$port"; then echo "  $name  UP    :$port"; else echo "  $name  DOWN  :$port"; fi
  done
}

case "${1:-}" in
  up)
    echo "Starting…"
    start_backend
    start_frontend
    echo "Open http://localhost:$FRONTEND_PORT  (logs: ./dev.sh logs)"
    ;;
  down)
    echo "Stopping…"
    stop_one frontend "$FRONTEND_PORT"
    stop_one backend "$BACKEND_PORT"
    ;;
  restart) "$0" down; "$0" up ;;
  status) status ;;
  logs)
    case "${2:-}" in
      backend|frontend) tail -f "$RUN_DIR/$2.log" ;;
      "") tail -f "$RUN_DIR/backend.log" "$RUN_DIR/frontend.log" ;;
      *) echo "logs: expected backend|frontend" >&2; exit 1 ;;
    esac
    ;;
  *)
    echo "usage: $0 {up|down|restart|status|logs [backend|frontend]}" >&2
    exit 1
    ;;
esac
