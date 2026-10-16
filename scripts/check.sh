#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="python"
SIM="sim"
if [[ -x "$ROOT/backend/.venv/bin/python" ]]; then
  PYTHON="$ROOT/backend/.venv/bin/python"
fi
if [[ -x "$ROOT/backend/.venv/bin/sim" ]]; then
  SIM="$ROOT/backend/.venv/bin/sim"
fi

(
  cd "$ROOT/backend"
  "$PYTHON" -m pip install -e ".[dev]"
  ruff check . ../scripts
  mypy simcore api cli
  pytest -q
  "$SIM" validate ../scenarios/_template
  "$SIM" validate ../scenarios/chongzhen_1627
  "$PYTHON" ../scripts/content_lint.py ../scenarios/_template --strict
  "$PYTHON" ../scripts/simulate.py ../scenarios/_template --seeds 20
)

(
  cd "$ROOT/frontend"
  npm ci
  npx tsc --noEmit
  npm run build
)
