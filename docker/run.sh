#!/usr/bin/env bash
# Build the clean-room image and run the smoke test.
#   docker/run.sh              # deterministic tests only (free, no auth)
#   (with CLAUDE_CODE_OAUTH_TOKEN in .env)  # + real `claude -p` end-to-end test
#
# Auth: Claude Code in the container authenticates via CLAUDE_CODE_OAUTH_TOKEN, sourced
# from the repo's .env (NEVER baked into the image — .dockerignore excludes .env). It is
# passed to the container at runtime only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE=astrojones-test

echo "building $IMAGE from $ROOT ..."
docker build -f "$ROOT/docker/Dockerfile" -t "$IMAGE" "$ROOT"

TOKEN=""
if [ -f "$ROOT/.env" ]; then
  # Source only CLAUDE_CODE_OAUTH_TOKEN (don't leak other .env vars into the container).
  TOKEN="$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' "$ROOT/.env" | head -1 | sed -E 's/^CLAUDE_CODE_OAUTH_TOKEN=//' | tr -d '"'"'" || true)"
fi

if [ -n "$TOKEN" ]; then
  echo "running with CLAUDE_CODE_OAUTH_TOKEN (full e2e) ..."
  docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" "$IMAGE"
else
  echo "running deterministic tests only (no CLAUDE_CODE_OAUTH_TOKEN in $ROOT/.env — the claude -p e2e test will skip) ..."
  docker run --rm "$IMAGE"
fi