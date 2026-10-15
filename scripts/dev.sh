#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="python"
if [[ -x "$ROOT/backend/.venv/bin/python" ]]; then
  PYTHON="$ROOT/backend/.venv/bin/python"
fi

(
  cd "$ROOT/backend"
  SIM_GAME_ROOT="$ROOT" "$PYTHON" -m uvicorn api.app:app --reload --host 127.0.0.1 --port 8000
) &
BACKEND_PID=$!

(
  cd "$ROOT/frontend"
  npm run dev -- --host 127.0.0.1
) &
FRONTEND_PID=$!

cleanup() {
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait
