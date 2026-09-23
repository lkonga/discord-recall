#!/usr/bin/env bash
# One gate for every commit: backend lint + tests, frontend lint + typecheck/build.
#
#   ./scripts/verify.sh          # everything
#   ./scripts/verify.sh --fast   # backend lint + tests only (skips the web build)
#
# Used by the pre-commit hook (see CONTRIBUTING.md) and by CI-by-hand.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
fast=false
[[ "${1:-}" == "--fast" ]] && fast=true

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "backend: ruff"
uv run ruff check src tests scripts

step "backend: pytest"
uv run python -m pytest tests/ -q

if $fast; then
  printf '\nfast mode: skipping the frontend build\n'
  exit 0
fi

if [[ -f web/package.json ]]; then
  step "web: eslint"
  (cd web && npm run lint)

  step "web: typecheck + build"
  (cd web && npm run build)
else
  printf '\nno web/package.json: skipping the frontend\n'
fi

printf '\n\033[32mall checks passed\033[0m\n'
