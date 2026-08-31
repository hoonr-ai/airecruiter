#!/bin/bash
# Install the dependencies a Claude Code on the web session needs to run this
# repo's own checks (apps/api pytest, apps/web eslint + node --test).
#
# Only runs in remote sessions — local machines keep their own setup.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

# ── apps/api (Python) ────────────────────────────────────────────────────────
# `--ignore-installed PyJWT`: the base image carries a distro-packaged PyJWT
# 2.7.0 with no RECORD file, so pip cannot uninstall it to satisfy our
# `PyJWT>=2.8.0` pin and the whole install aborts with
#   "Cannot uninstall PyJWT 2.7.0, RECORD file not found."
# Ignoring the distro copy lets pip drop 2.13.0 alongside it instead.
python3 -m pip install --quiet --disable-pip-version-check \
  --ignore-installed PyJWT \
  -r "$ROOT/apps/api/requirements.txt" \
  -r "$ROOT/apps/api/requirements-dev.txt"

# Tests import `services.*` / `core.*` relative to apps/api.
echo "export PYTHONPATH=\"$ROOT/apps/api\"" >> "$CLAUDE_ENV_FILE"

# ── apps/web (Node) ──────────────────────────────────────────────────────────
# `install`, not `ci`: the container image is cached after this hook, and
# install reuses whatever node_modules the cache already holds.
# `--legacy-peer-deps` is explicit rather than inherited from the root .npmrc:
# @azure/msal-react@2 vs the React version here is a real ERESOLVE conflict,
# and npm resolves project config against the package dir, so a `--prefix`
# install (or any cwd outside the repo root) would miss that setting.
cd "$ROOT/apps/web"
npm install --legacy-peer-deps --no-audit --no-fund

echo "session-start: dependencies installed"
