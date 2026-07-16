# syntax=docker/dockerfile:1
# Clean-room test harness for the astrojones plugin.
#
# Goal: a container with ONLY Claude Code + the astrojones plugin — no host config,
# no cognee creds, no competing memory plugin — to prove the plugin works standalone
# (MCP server auto-connects, safety hooks fire, session_start fails open without a
# memory backend). This is the "does it actually work in a vanilla env?" test that the
# host install — polluted with cognee-memory, lean-ctx, and 20+ other plugins — cannot
# answer.
#
# Build:  docker build -t astrojones-test .
#   Run:  docker run --rm astrojones-test                                                  # deterministic tests (free)
#         docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN=... astrojones-test                   # + real claude -p e2e
#         (docker/run.sh sources CLAUDE_CODE_OAUTH_TOKEN from the repo's .env automatically)
#
# Python policy: NO system python is installed (nothing to break under PEP 668). uv
# manages the Python 3.13 the harness requires (pyproject pins >=3.13,<3.15). `python3`
# is symlinked to that managed interpreter ONLY so the plugin's hooks.json commands
# (which hardcode `python3 ...`) resolve — our own scripts call the venv python
# explicitly, never bare `python3`.
# node:24 = active LTS (Oct 2025+). node:26-slim also works if you want the absolute latest.
FROM node:24-slim

# System deps:
#   git  — the harness is git-repo-aware (git ls-files, repo_root, status).
#   curl — uv installer.
#   jq   — result inspection.
#   NO python3 here — uv provides the interpreter (see policy note above).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl jq \
    && rm -rf /var/lib/apt/lists/*

# uv: fetches + manages the Python 3.13 the harness requires. Copied to /usr/local/bin
# so the non-root runtime user can also call it (the hook shim's cold-fallback path does
# `uv run ...`, and `uv sync` built the venv at image time).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && cp /root/.local/bin/uv  /usr/local/bin/uv \
 && cp /root/.local/bin/uvx /usr/local/bin/uvx

# Claude Code CLI — vanilla. No plugins, no settings, no marketplace state baked in.
RUN npm install -g @anthropic-ai/claude-code@latest

# Non-root runtime user.
RUN useradd -m -s /bin/bash coder

# The plugin, copied in. Trusted by construction: this is the same code the MCP server
# runs and the same code the hook shims resolve to.
COPY --chown=coder:coder . /plugins/astrojones

USER coder
# Make the plugin a git repo (a bare `git init` — no commits needed). The host plugin
# checkout is a git repo, and the plugin's own integration tests assume that: e.g.
# test_drift's `repo_drift_check()` calls `git.repo_root()`, which returns None (→ a
# `_no_repo()` error dict with no "ok" key → KeyError) when the plugin isn't in a git
# repo. `.dockerignore` strips the host's `.git`, so we re-init here to mirror the dev
# env the tests were written for. (Side finding: `repo_drift_check` is effectively
# broken for non-git plugin installs, e.g. marketplace installs into ~/.claude/plugins.)
RUN git init -q /plugins/astrojones

# Pre-build the harness venv AS CODER (not root) so uv installs the managed Python 3.13
# into /home/coder/.local — a path coder can traverse. Building as root puts the managed
# interpreter under /root/.local (mode 700), and the venv's bin/python symlinks there;
# chowning .venv does NOT follow that symlink, so coder hits "Permission denied" (the
# exact failure of the first two smoke runs). Warm venv => no ~30s cold `uv run` at the
# first turn.
RUN uv sync --project /plugins/astrojones/servers/harness-mcp

# Back to root to write the python3 symlink into /usr/local/bin (so the plugin's
# hooks.json — which hardcodes `python3 ...` — resolves to the venv's 3.13). The symlink
# target is now coder-owned/accessible, so coder executing `python3` works.
USER root
RUN ln -sf /plugins/astrojones/servers/harness-mcp/.venv/bin/python /usr/local/bin/python3 \
 && ln -sf /usr/local/bin/python3 /usr/local/bin/python

USER coder
WORKDIR /workspace
ENV CLAUDE_PLUGIN_ROOT=/plugins/astrojones
# Marks the runtime for docker/test.sh's host-safety guard (belt to the /.dockerenv braces).
ENV ASTROJONES_TEST_IN_CONTAINER=1

ENTRYPOINT ["/plugins/astrojones/docker/test.sh"]