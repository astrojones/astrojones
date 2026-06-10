# uv-based build. The src/-layout package is installed by `uv sync`.
# Default 3.14; drop to python3.13-bookworm-slim if a dep isn't 3.14-ready yet
# (keep this in sync with .python-version and requires-python).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /app

# Copy metadata + lock first for layer caching, then the source.
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

EXPOSE 8080
CMD ["uv", "run", "--no-dev", "uvicorn", "__REPO_PKG__.api:app", "--host", "0.0.0.0", "--port", "8080"]
